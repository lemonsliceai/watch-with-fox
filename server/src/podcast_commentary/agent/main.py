"""Agent entrypoint — wires up the LiveKit session and dispatches Fox.

This module is intentionally thin: all conversation behaviour lives in
`ComedianAgent` and its collaborators (see `comedian.py`, `speech_gate.py`,
`user_turn.py`, `podcast_pipeline.py`). Here we only:

  * build the `AgentSession` (STT / LLM / TTS / VAD / turn detection)
  * start the LemonSlice avatar (if one was requested)
  * extract the podcast audio URL via yt-dlp
  * construct the `ComedianAgent` and start the session
  * register a shutdown hook that tears the podcast pipeline down

Splitting these concerns out of the agent itself keeps the composition root
small enough to reason about, and lets the agent class stay focused on
commentary behaviour rather than server plumbing.
"""

import asyncio
import json
import logging
import time
import uuid
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    room_io,
)
from livekit.plugins import elevenlabs, groq, lemonslice, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from podcast_commentary.agent.comedian import ComedianAgent
from podcast_commentary.agent.prompts import COMEDIAN_SYSTEM_PROMPT
from podcast_commentary.core.config import settings
from podcast_commentary.core.youtube import extract_audio_url, is_youtube_url

logger = logging.getLogger("podcast-commentary.agent")

load_dotenv()
load_dotenv(".env.local", override=True)

# Callum — husky trickster voice; picked for comedic timing.
VOICE_ID = "N2lVS1w4EtoT3dr4eOWO"


server = AgentServer(num_idle_processes=2)


def prewarm(proc: JobProcess) -> None:
    """Preload Silero VAD so the first session doesn't pay the cost."""
    proc.userdata["vad"] = silero.VAD.load(activation_threshold=0.6)


server.setup_fnc = prewarm


def _make_sticky_proxy() -> str | None:
    """Pin the rotating residential proxy to one exit IP for this job.

    IPRoyal rotates exit IPs per TCP connection. YouTube signed URLs embed
    ``ip=…`` of the requester, so yt-dlp and ffmpeg **must** egress from
    the same IP. Appending ``_session-…_lifetime-30m`` to the **password**
    (IPRoyal's convention) pins the exit IP for 30 minutes — long enough
    for any single podcast session.

    Each job gets a unique session ID so concurrent sessions don't share
    (and therefore contend over) the same proxy IP.
    """
    proxy = settings.YOUTUBE_PROXY
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.password or not parsed.hostname:
        return proxy
    # Only inject session params for known rotating-residential providers.
    if "iproyal.com" not in parsed.hostname:
        return proxy
    session_id = uuid.uuid4().hex[:8]
    new_password = f"{parsed.password}_session-{session_id}_lifetime-30m"
    netloc = f"{parsed.username}:{new_password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    sticky = urlunparse(parsed._replace(netloc=netloc))
    logger.info(
        "Sticky proxy session created (session=%s, provider=%s)",
        session_id, parsed.hostname,
    )
    return sticky


def _parse_job_metadata(ctx: JobContext) -> dict:
    if not ctx.job.metadata:
        return {}
    try:
        return json.loads(ctx.job.metadata)
    except json.JSONDecodeError:
        logger.warning("Failed to parse job metadata: %s", ctx.job.metadata)
        return {}


def _build_session(vad) -> AgentSession:
    """Assemble the AgentSession with STT / LLM / TTS / turn detection.

    Notes:
      * `preemptive_generation=False` — we control exactly when Fox
        speaks; no speculative generation.
      * `resume_false_interruption=False` — the avatar path sets
        `audio_output=False`, whose audio sink doesn't implement
        `.can_pause`; resume would log a warning and no-op.
      * Session-level `allow_interruptions` stays at its default (True).
        Setting it False globally broke the intro via the LemonSlice
        avatar (the aec-warmup path stalls when interruptions are off).
        We enforce non-interruption per-turn via `SpeechGate.speak`.
    """
    return AgentSession(
        stt=groq.STT(model="whisper-large-v3-turbo"),
        llm=groq.LLM(model="meta-llama/llama-4-scout-17b-16e-instruct"),
        tts=elevenlabs.TTS(
            model="eleven_turbo_v2_5",
            voice_id=VOICE_ID,
            voice_settings=elevenlabs.VoiceSettings(
                stability=0.4,
                similarity_boost=0.7,
                speed=1.05,
            ),
        ),
        turn_detection=MultilingualModel(),
        vad=vad,
        preemptive_generation=False,
        resume_false_interruption=False,
    )


async def _start_avatar(
    metadata: dict, session: AgentSession, ctx: JobContext
) -> str | None:
    """Start the LemonSlice avatar if one was configured."""
    avatar_url = metadata.get("avatar_url")
    if not avatar_url:
        logger.info("No avatar_url — skipping avatar")
        return None

    avatar = lemonslice.AvatarSession(
        agent_image_url=avatar_url,
        agent_prompt=(
            "an anthropomorphic fox comedian reacting to a video, animated "
            "facial expressions, occasionally laughing"
        ),
        agent_idle_prompt=(
            "an anthropomorphic fox listening intently with occasional subtle "
            "reactions and smirks"
        ),
    )
    try:
        t0 = time.perf_counter()
        session_id = await avatar.start(session, room=ctx.room)
        logger.info("Avatar started in %.2fs", time.perf_counter() - t0)
        return session_id
    except Exception:
        logger.warning("Avatar failed to start — continuing audio only", exc_info=True)
        return None


async def _wait_for_avatar_participant(room, timeout: float = 15.0) -> bool:
    identity = "lemonslice-avatar-agent"
    ready = asyncio.Event()

    def _on_participant(participant):
        if participant.identity == identity:
            ready.set()

    room.on("participant_connected", _on_participant)
    for p in room.remote_participants.values():
        if p.identity == identity:
            ready.set()

    if ready.is_set():
        return True
    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
        return True
    except TimeoutError:
        logger.warning("Avatar participant did not connect within %.0fs", timeout)
        return False


async def _extract_audio_url(
    video_url: str | None, proxy: str | None = None,
) -> str | None:
    """Resolve a direct-CDN audio URL using yt-dlp.

    IMPORTANT: this MUST run in the same process (same IP) that will later
    fetch the URL with ffmpeg. YouTube's signed URLs embed `ip=…` of the
    requesting host; if the API server extracts and the agent fetches, the
    CDN returns 403. Keeping extraction in the agent process makes the
    signatures match.

    When a sticky *proxy* is supplied, yt-dlp routes through it so the
    signed URL's ``ip=`` matches what ffmpeg will use later.
    """
    if not video_url:
        logger.warning(
            "!! No video_url in job metadata — agent will have no podcast "
            "audio. Check that the API server is setting metadata.video_url "
            "on dispatch."
        )
        return None
    if not is_youtube_url(video_url):
        logger.warning(
            "!! video_url %r is not a recognised YouTube URL — agent cannot "
            "extract audio", video_url,
        )
        return None
    logger.info("Extracting audio URL (in agent process) for %s", video_url)
    t0 = time.perf_counter()
    url = await extract_audio_url(video_url, proxy=proxy)
    elapsed = time.perf_counter() - t0
    if url:
        logger.info("yt-dlp extraction succeeded in %.2fs", elapsed)
    else:
        logger.error(
            "!! yt-dlp extraction FAILED in %.2fs for %s — podcast audio "
            "pipeline will NOT start. Fox will hear nothing. See earlier "
            "yt-dlp logs for the underlying reason (403 / client block / "
            "signature).",
            elapsed, video_url,
        )
    return url


@server.rtc_session(agent_name=settings.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    """Per-job entrypoint — called by the LiveKit agent worker."""
    metadata = _parse_job_metadata(ctx)

    # Connect to the room BEFORE session.start() so `local_participant` is
    # usable inside `on_enter` (otherwise publish_data raises "cannot access
    # local participant before connecting").
    await ctx.connect()

    # Pin the residential proxy to a single exit IP for this job so yt-dlp
    # and ffmpeg share the same IP (YouTube signed URLs are IP-locked).
    sticky_proxy = _make_sticky_proxy()

    # Extract audio URL concurrently with avatar setup so we don't add
    # yt-dlp latency to session start.
    audio_url_task = asyncio.create_task(
        _extract_audio_url(metadata.get("video_url"), proxy=sticky_proxy)
    )

    session = _build_session(vad=ctx.proc.userdata["vad"])
    avatar_session_id = await _start_avatar(metadata, session, ctx)

    try:
        audio_url = await audio_url_task
    except Exception:
        logger.exception("yt-dlp extraction task failed")
        audio_url = None

    agent = ComedianAgent(
        instructions=COMEDIAN_SYSTEM_PROMPT,
        audio_url=audio_url,
        # Share the prewarmed VAD between session STT and podcast STT — VAD
        # instances support multiple concurrent streams.
        vad=ctx.proc.userdata["vad"],
        # Supplied by the API server when the session row is created. Used
        # to thread every turn (podcast / user / agent) + the rolling
        # summary into the conversation_messages table.
        session_id=metadata.get("session_id"),
        # Sticky proxy — same exit IP for both yt-dlp extraction and ffmpeg
        # streaming so the YouTube CDN honours the signed URL.
        proxy=sticky_proxy,
    )

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_output=False if avatar_session_id else True,
        ),
    )

    ctx.add_shutdown_callback(agent.shutdown)

    if avatar_session_id:
        await _wait_for_avatar_participant(ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
