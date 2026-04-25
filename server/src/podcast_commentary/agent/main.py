"""Agent entrypoint — wires up N PersonaAgents + a Director per job.

This module is intentionally thin: all conversation behaviour lives in
``PersonaAgent`` (per-persona) and ``Director`` (room-wide
orchestration). Here we only:

  * parse the job metadata (which personas + per-persona avatar URLs)
  * for each persona, build an ``AgentSession`` (STT / LLM / TTS / VAD /
    turn detection from the persona's own ``FoxConfig``)
  * start the LemonSlice avatar with a *unique* participant identity per
    persona so multiple avatars can coexist in the room
  * construct the ``Director``, hand it the personas, and wait for every
    persona's ``ready`` event before delivering coordinated intros
"""

import asyncio
import json
import logging
import time
from typing import Any

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

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.director import Director
from podcast_commentary.agent.fox_config import FoxConfig, load_config
from podcast_commentary.core.config import settings

logger = logging.getLogger("podcast-commentary.agent")

load_dotenv()
load_dotenv(".env.local", override=True)


server = AgentServer(num_idle_processes=2)


def prewarm(proc: JobProcess) -> None:
    """Preload Silero VAD once per worker process.

    All personas in a job share this single VAD instance — Silero is
    stateless across calls, so sharing is safe and saves the ~80 MB
    per-instance model load.
    """
    proc.userdata["vad"] = silero.VAD.load(activation_threshold=0.5)


server.setup_fnc = prewarm


def _parse_job_metadata(ctx: JobContext) -> dict:
    if not ctx.job.metadata:
        return {}
    try:
        return json.loads(ctx.job.metadata)
    except json.JSONDecodeError:
        logger.warning("Failed to parse job metadata: %s", ctx.job.metadata)
        return {}


def _resolve_personas(metadata: dict) -> list[dict[str, str]]:
    """Return the persona descriptors (name + avatar_url) for this job.

    The API server includes a ``personas`` list in metadata. We fall back
    to building one from ``settings.PERSONAS`` and each preset's own
    ``AvatarConfig.avatar_url`` so a stale API still functions during a
    rolling deploy.
    """
    personas = metadata.get("personas")
    if isinstance(personas, list) and personas:
        return personas

    descriptors: list[dict[str, str]] = []
    for name in (settings.PERSONAS or "fox").split(","):
        name = name.strip()
        if not name:
            continue
        cfg = load_config(name)
        descriptors.append({"name": name, "label": name, "avatar_url": cfg.avatar.avatar_url})
    return descriptors


def _build_session(config: FoxConfig, vad: Any) -> AgentSession:
    """Build one AgentSession from a persona's FoxConfig.

    Notes:
      * ``preemptive_generation=False`` — we control exactly when each
        persona speaks; no speculative generation.
      * ``resume_false_interruption=False`` — the avatar path sets
        ``audio_output=False``, whose audio sink doesn't implement
        ``.can_pause``; resume would log a warning and no-op.
      * ``allow_interruptions=False`` at the session level so VAD-triggered
        interruption never fires. The ONLY way a persona mid-utterance
        gets silenced is the user pressing "Skip commentary" — that routes
        through ``Director._handle_skip`` which calls
        ``SpeechGate.interrupt(force=True)`` explicitly.
    """
    return AgentSession(
        stt=groq.STT(model=config.stt.model),
        llm=groq.LLM(
            model=config.llm.model,
            max_completion_tokens=config.llm.max_tokens,
        ),
        tts=elevenlabs.TTS(
            model=config.tts.model,
            voice_id=config.tts.voice_id,
            voice_settings=elevenlabs.VoiceSettings(
                stability=config.tts.stability,
                similarity_boost=config.tts.similarity_boost,
                speed=config.tts.speed,
            ),
        ),
        turn_detection=MultilingualModel(),
        vad=vad,
        preemptive_generation=False,
        resume_false_interruption=False,
        allow_interruptions=False,
    )


async def _start_avatar(
    *,
    config: FoxConfig,
    avatar_url: str | None,
    session: AgentSession,
    ctx: JobContext,
    identity: str,
) -> str | None:
    """Start the LemonSlice avatar for one persona under a unique identity.

    Returns the avatar session id (LemonSlice's internal handle) on
    success, or None if no avatar was configured / startup failed. We
    swallow startup failures so a single broken avatar doesn't kill the
    whole show — the persona can still speak audio-only.
    """
    if not avatar_url:
        logger.info("[%s] No avatar_url — skipping avatar", config.name)
        return None

    avatar = lemonslice.AvatarSession(
        agent_image_url=avatar_url,
        agent_prompt=config.avatar.active_prompt,
        agent_idle_prompt=config.avatar.idle_prompt,
        avatar_participant_identity=identity,
    )
    try:
        t0 = time.perf_counter()
        session_id = await avatar.start(session, room=ctx.room)
        logger.info("[%s] Avatar started in %.2fs", config.name, time.perf_counter() - t0)
        return session_id
    except Exception:
        logger.warning(
            "[%s] Avatar failed to start — continuing audio only",
            config.name,
            exc_info=True,
        )
        return None


def _avatar_identity_for(persona_name: str) -> str:
    """Per-persona avatar participant identity.

    Each LemonSlice instance must publish under a unique identity or
    LiveKit treats them as the same participant and only one set of
    tracks survives. The Chrome extension routes incoming tracks by
    matching this prefix — see ``sidepanel.js``.
    """
    return f"lemonslice-avatar-{persona_name}"


# Track-name prefix the extension uses to peel a persona off a direct-publish
# audio track. Each audio-only persona publishes under
# ``persona-<name>`` (e.g. ``persona-fox``) so the side-panel can route both
# tracks separately even though they share one ``local_participant``.
_PERSONA_TRACK_PREFIX = "persona-"


def _persona_track_name(persona_name: str) -> str:
    return f"{_PERSONA_TRACK_PREFIX}{persona_name}"


@server.rtc_session(agent_name=settings.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    """Per-job entrypoint — called by the LiveKit agent worker."""
    metadata = _parse_job_metadata(ctx)
    persona_descriptors = _resolve_personas(metadata)
    if not persona_descriptors:
        logger.error("No personas resolved for job — aborting")
        return

    # Connect BEFORE starting any session so local_participant is usable
    # inside Director.start() (publish_data needs it).
    await ctx.connect()

    vad = ctx.proc.userdata["vad"]
    session_id = metadata.get("session_id")

    personas: list[PersonaAgent] = []
    avatar_identities: dict[str, str] = {}

    for descriptor in persona_descriptors:
        name = descriptor["name"]
        avatar_url = descriptor.get("avatar_url")
        config = load_config(name)

        logger.info(
            "[%s] === SYSTEM PROMPT ===\n%s\n=== END SYSTEM PROMPT ===",
            name,
            config.persona.system_prompt,
        )

        session = _build_session(config, vad=vad)

        identity = _avatar_identity_for(name)
        avatar_session_id = await _start_avatar(
            config=config,
            avatar_url=avatar_url,
            session=session,
            ctx=ctx,
            identity=identity,
        )
        # Only register the identity when an avatar was actually started —
        # otherwise the Director would block for the full
        # ``avatar.startup_timeout_s`` waiting on a participant that will
        # never publish, and the intro gets skipped. Audio-only personas
        # must still deliver their intro.
        if avatar_session_id is not None:
            avatar_identities[name] = identity

        persona = PersonaAgent(config=config, session_id=session_id)
        personas.append(persona)

        # When an avatar is active it owns audio output (LemonSlice
        # republishes the TTS audio lip-synced); enabling the session's
        # own audio track would double-publish. With no avatar, the
        # session MUST publish audio itself or TTS has no sink and every
        # speech handle resolves instantly with zero bytes on the wire.
        has_avatar = avatar_session_id is not None
        if has_avatar:
            audio_output: room_io.AudioOutputOptions | bool = False
        else:
            # All audio-only personas share `ctx.room.local_participant`,
            # so the default track name "roomio_audio" would collide and
            # the side-panel can't tell whose voice is whose. Give each
            # persona's published track a unique name so the extension
            # routes each one to its own audio-graph node.
            audio_output = room_io.AudioOutputOptions(track_name=_persona_track_name(name))
        await session.start(
            agent=persona,
            room=ctx.room,
            room_options=room_io.RoomOptions(
                audio_input=False,
                audio_output=audio_output,
                # Don't auto-close the AgentSession on a participant
                # disconnect. A brief extension reconnect (tab audio
                # hiccup, side-panel refresh) would otherwise kill the
                # session mid-turn. The Director's own disconnect handler
                # is the single authoritative teardown path.
                close_on_disconnect=False,
            ),
        )

    # Wait for every persona's on_enter to compose its SpeechGate.
    await asyncio.gather(*(p.ready.wait() for p in personas))

    # When the user disconnects, the framework auto-closes the AgentSessions
    # but the *job* keeps running — so the Director's background loops would
    # keep firing into dead sessions. Hand the Director a callback that ends
    # the job so the next call dispatches into a clean worker.
    async def _end_job_on_user_disconnect() -> None:
        logger.info("User disconnect → requesting job shutdown")
        try:
            shutdown = getattr(ctx, "shutdown", None)
            if shutdown is None:
                return
            result = shutdown(reason="user_disconnected")
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning("ctx.shutdown raised", exc_info=True)

    # Director takes over: intros, speaker selection, commentary cadence.
    # It owns per-persona avatar-readiness gating so one slow avatar can't
    # stall the other persona's intro (or the room entirely).
    director = Director(
        personas=personas,
        room=ctx.room,
        avatar_identities=avatar_identities,
        session_id=session_id,
        on_user_disconnect=_end_job_on_user_disconnect,
    )

    # Register the teardown hook BEFORE starting so a crash mid-start still
    # triggers the full Director shutdown (podcast pipeline, bg tasks, etc.)
    # instead of leaking them into the worker.
    ctx.add_shutdown_callback(director.shutdown)

    await director.start()


if __name__ == "__main__":
    cli.run_app(server)
