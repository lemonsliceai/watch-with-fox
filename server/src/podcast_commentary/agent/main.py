"""Agent entrypoint — wires up the LiveKit session and dispatches Fox.

This module is intentionally thin: all conversation behaviour lives in
`ComedianAgent` and its collaborators (see `comedian.py`, `speech_gate.py`,
`user_turn.py`, `podcast_pipeline.py`). Here we only:

  * build the `AgentSession` (STT / LLM / TTS / VAD / turn detection)
  * start the LemonSlice avatar (if one was requested)
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
from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.prompts import COMEDIAN_SYSTEM_PROMPT
from podcast_commentary.core.config import settings

logger = logging.getLogger("podcast-commentary.agent")

load_dotenv()
load_dotenv(".env.local", override=True)


server = AgentServer(num_idle_processes=2)


def prewarm(proc: JobProcess) -> None:
    """Preload Silero VAD so the first session doesn't pay the cost."""
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=CONFIG.vad.activation_threshold,
    )


server.setup_fnc = prewarm


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
        stt=groq.STT(model=CONFIG.stt.model),
        llm=groq.LLM(
            model=CONFIG.llm.model,
            max_completion_tokens=CONFIG.llm.max_tokens,
        ),
        tts=elevenlabs.TTS(
            model=CONFIG.tts.model,
            voice_id=CONFIG.tts.voice_id,
            voice_settings=elevenlabs.VoiceSettings(
                stability=CONFIG.tts.stability,
                similarity_boost=CONFIG.tts.similarity_boost,
                speed=CONFIG.tts.speed,
            ),
        ),
        turn_detection=MultilingualModel(),
        vad=vad,
        preemptive_generation=False,
        resume_false_interruption=False,
    )


async def _start_avatar(metadata: dict, session: AgentSession, ctx: JobContext) -> str | None:
    """Start the LemonSlice avatar if one was configured."""
    avatar_url = metadata.get("avatar_url")
    if not avatar_url:
        logger.info("No avatar_url — skipping avatar")
        return None

    avatar = lemonslice.AvatarSession(
        agent_image_url=avatar_url,
        agent_prompt=CONFIG.avatar.active_prompt,
        agent_idle_prompt=CONFIG.avatar.idle_prompt,
    )
    try:
        t0 = time.perf_counter()
        session_id = await avatar.start(session, room=ctx.room)
        logger.info("Avatar started in %.2fs", time.perf_counter() - t0)
        return session_id
    except Exception:
        logger.warning("Avatar failed to start — continuing audio only", exc_info=True)
        return None


async def _wait_for_avatar_participant(
    room,
    timeout: float = CONFIG.avatar.startup_timeout_s,
) -> bool:
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


@server.rtc_session(agent_name=settings.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    """Per-job entrypoint — called by the LiveKit agent worker."""
    metadata = _parse_job_metadata(ctx)

    # Connect to the room BEFORE session.start() so `local_participant` is
    # usable inside `on_enter` (otherwise publish_data raises "cannot access
    # local participant before connecting").
    await ctx.connect()

    session = _build_session(vad=ctx.proc.userdata["vad"])
    avatar_session_id = await _start_avatar(metadata, session, ctx)

    logger.info(
        "=== FOX SYSTEM PROMPT ===\n%s\n=== END SYSTEM PROMPT ===",
        COMEDIAN_SYSTEM_PROMPT,
    )

    agent = ComedianAgent(
        instructions=COMEDIAN_SYSTEM_PROMPT,
        # Supplied by the API server when the session row is created. Used
        # to thread every turn (podcast / user / agent) + the rolling
        # summary into the conversation_messages table.
        session_id=metadata.get("session_id"),
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
