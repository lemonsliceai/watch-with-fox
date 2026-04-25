"""Director — wires the room components together and forwards persona events.

The Director is a Facade over the orchestration components: it owns the
construction graph, the room-coupled glue (track replay, user disconnect),
the lifecycle entrypoints (``start`` / ``shutdown``), and a Mediator-style
trio of persona-event shims that fan out to the right component without
exposing each component to ``PersonaAgent``'s private callback attrs.

Component responsibilities — see each module for detail:

  * :class:`TaskSupervisor` — fire-and-forget tracking + bulk cancel
  * :class:`RoomState`      — shutdown/intros events, monotonic clock,
                              ``is_listening`` predicate
  * :class:`ControlChannel` — commentary.control I/O (publish + dispatch)
  * :class:`PlayoutWaiter`  — robust playout wait with vendor-RPC recovery
  * :class:`IntroSequencer` — sequenced, avatar-readiness-gated intros
  * :class:`CommentaryPipeline` — single-flight selector → delivery
  * :class:`CommentaryScheduler` — silence loop, watchdog, kickoff, sentence
  * :class:`SettingsController` — frequency/length presets

This file is intentionally the only place that knows there's *more than
one* PersonaAgent in the room and the only place that wires components
together. Each PersonaAgent stays oblivious — it just speaks when
``deliver_commentary`` is called on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from livekit import rtc

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.commentary import CommentaryTimer, FullTranscript
from podcast_commentary.agent.commentary_pipeline import CommentaryPipeline
from podcast_commentary.agent.commentary_scheduler import (
    SILENCE_FALLBACK_DELAY,
    CommentaryScheduler,
)
from podcast_commentary.agent.control_channel import ControlChannel
from podcast_commentary.agent.intro_sequencer import IntroSequencer
from podcast_commentary.agent.playout_waiter import PlayoutWaiter
from podcast_commentary.agent.podcast_pipeline import PodcastPipeline
from podcast_commentary.agent.room_state import RoomState
from podcast_commentary.agent.selector import SpeakerSelector
from podcast_commentary.agent.settings_controller import SettingsController
from podcast_commentary.agent.skip_coordinator import SkipCoordinator
from podcast_commentary.agent.task_supervisor import TaskSupervisor
from podcast_commentary.core.config import settings
from podcast_commentary.core.db import log_conversation_message

logger = logging.getLogger("podcast-commentary.director")


# Avatar participants publish under this identity prefix (see main.py
# ``_avatar_identity_for``). Anything else disconnecting is the user.
_AVATAR_IDENTITY_PREFIX = "lemonslice-avatar-"


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Fire-and-forget task %r failed: %s", task.get_name(), exc, exc_info=exc)


class Director:
    """One Director per room. Lives for the whole job.

    Construction is cheap; real work begins on ``start()`` (after every
    persona's ``ready`` Event has fired).
    """

    def __init__(
        self,
        *,
        personas: list[PersonaAgent],
        room: rtc.Room,
        avatar_identities: dict[str, str] | None = None,
        session_id: str | None = None,
        on_user_disconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not personas:
            raise ValueError("Director needs at least one PersonaAgent")
        self._personas = personas
        self._room = room
        self._session_id = session_id
        # Called once, after shutdown, when the *user* (not an avatar)
        # leaves the room. Lets main.py terminate the job so a new call
        # dispatches into a fresh worker instead of inheriting zombie state.
        self._on_user_disconnect = on_user_disconnect

        # Foundational services with no orchestration deps.
        self._tasks = TaskSupervisor()
        self._room_state = RoomState(personas)
        self._timer = CommentaryTimer()
        self._full_transcript = FullTranscript()
        self._podcast = PodcastPipeline(on_transcript=self._handle_podcast_transcript)
        self._selector = SpeakerSelector(
            model=settings.DIRECTOR_LLM_MODEL,
            max_consecutive=settings.DIRECTOR_MAX_CONSECUTIVE,
        )
        self._skip = SkipCoordinator(personas)
        self._control = ControlChannel(room)
        self._playout = PlayoutWaiter()

        # Orchestration components.
        self._intros = IntroSequencer(
            personas=personas,
            room=room,
            avatar_identities=dict(avatar_identities or {}),
            room_state=self._room_state,
            control=self._control,
            playout_waiter=self._playout,
        )
        self._pipeline = CommentaryPipeline(
            personas=personas,
            room_state=self._room_state,
            timer=self._timer,
            full_transcript=self._full_transcript,
            selector=self._selector,
            control=self._control,
            playout_waiter=self._playout,
        )
        self._scheduler = CommentaryScheduler(
            pipeline=self._pipeline,
            room_state=self._room_state,
            timer=self._timer,
            full_transcript=self._full_transcript,
            tasks=self._tasks,
        )
        self._settings = SettingsController(
            timer=self._timer,
            personas=personas,
            base_silence_delay=SILENCE_FALLBACK_DELAY,
            apply_silence_delay=self._scheduler.set_silence_delay,
        )

        # Inbound control wiring.
        self._control.register("skip", self._handle_skip)
        self._control.register("settings", self._handle_settings)

        self._shutting_down: bool = False
        # Tracked separately from the supervisor so a self-triggered
        # shutdown (user disconnect) doesn't cancel itself mid-await.
        self._shutdown_task: asyncio.Task | None = None

        # Mediator: each persona's events fan out through us so other
        # components don't reach into PersonaAgent's private callback attrs.
        self._wire_persona_callbacks()

    # ==================================================================
    # Lifecycle
    # ==================================================================
    async def start(self) -> None:
        """Begin the show: deliver intros, attach STT, kick the silence loop.

        Caller must wait for every persona's ``ready`` Event before
        calling this — otherwise SpeechGate is None and ``deliver_intro``
        would crash.
        """
        # Podcast pipeline must start BEFORE we wire the room listener /
        # replay tracks. If the extension's podcast-audio track is already
        # subscribed by the time we get here, ``attach_track`` needs the
        # frame buffer to already exist — otherwise the track is dropped
        # on the floor and the agent never hears the video.
        self._podcast.start()
        self._wire_room_listeners()
        self._replay_existing_tracks()
        self._playout.attach_observers(self._personas)

        # Intros run in the background. When the second avatar's
        # ``lk.playback_finished`` RPC is dropped (livekit/agents #3510)
        # the intro handle hangs for the full ``intro_timeout_s`` before
        # the synthesized fallback wakes it; awaiting that inline would
        # freeze the whole room. The silence loop, watchdog, and
        # commentary paths all guard on ``RoomState.is_listening()`` so a
        # pending intro keeps them quiet without keeping them unstarted.
        self._tasks.fire_and_forget(self._run_intro_sequence(), name="director_intros")
        self._scheduler.start()

    async def _run_intro_sequence(self) -> None:
        await self._intros.run()
        if self._room_state.shutting_down:
            return
        await self._control.publish_agent_ready(
            [{"name": p.name, "label": p.label} for p in self._personas]
        )
        await self._scheduler.post_intro_kickoff()

    async def shutdown(self) -> None:
        """Tear down all Director-owned work. Idempotent.

        Ordering matters: flip ``_shutting_down`` and ``RoomState`` first
        so any in-flight commentary path sees the flag and early-exits;
        interrupt live speech handles so stale avatar playouts don't
        linger; then cancel every tracked background task and await them;
        finally stop the podcast STT pipeline.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        self._room_state.mark_shutdown()
        logger.info("Director shutting down")

        # Interrupt anyone mid-utterance so the framework's
        # ``clear_buffer`` RPC fires while the room transport is still up.
        for persona in self._personas:
            with contextlib.suppress(Exception):
                persona.interrupt()

        await self._tasks.shutdown()

        with contextlib.suppress(Exception):
            await self._podcast.shutdown()

    # ==================================================================
    # Persona event shims (Mediator)
    # ==================================================================
    def _wire_persona_callbacks(self) -> None:
        for p in self._personas:
            p._on_speech_start_cb = self._on_persona_speech_start  # type: ignore[attr-defined]
            p._on_speech_end_cb = self._on_persona_speech_end  # type: ignore[attr-defined]
            p._on_turn_finalised_cb = self._on_persona_turn_finalised  # type: ignore[attr-defined]

    def _on_persona_speech_start(self, persona: PersonaAgent) -> None:
        """Real audio just started reaching the avatar."""
        self._timer.record_speech_start()

    def _on_persona_speech_end(self, persona: PersonaAgent) -> None:
        """Real audio finished — re-arm the silence loop.

        ``commentary_end`` is NOT published here; the delivery paths
        (``CommentaryPipeline._deliver`` and ``IntroSequencer._speak_intro_with_timeout``)
        each publish it in their own ``finally`` block. That keeps a
        single authoritative emitter and avoids dropping the event when
        the framework's ``agent_state_changed: speaking→listening``
        transition never fires — e.g. the LemonSlice second-avatar
        ``lk.playback_finished`` RPC went missing and the playout-waiter
        had to synthesise completion itself.
        """
        if self._shutting_down:
            return
        if self._room_state.is_listening():
            self._scheduler.rearm_silence()

    async def _on_persona_turn_finalised(
        self, persona: PersonaAgent, text: str, angle: str | None
    ) -> None:
        """Persona's assistant message landed — record it for the room."""
        logger.info(
            "Director recorded %s turn (angle=%s, lines_history=%d)",
            persona.name,
            angle,
            len(persona.commentary_history),
        )

    # ==================================================================
    # Podcast STT → commentary trigger
    # ==================================================================
    async def _handle_podcast_transcript(self, text: str) -> None:
        """Called by PodcastPipeline for every podcast STT result."""
        self._persist("podcast", text, None)
        sentence_count = self._full_transcript.add(text)
        self._scheduler.maybe_trigger_on_sentence(sentence_count)

    # ==================================================================
    # Room listeners (track replay + user disconnect)
    # ==================================================================
    def _wire_room_listeners(self) -> None:
        self._control.attach()
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("participant_disconnected", self._on_participant_disconnected)

    def _replay_existing_tracks(self) -> None:
        """Replay track_subscribed for tracks present before we subscribed.

        The Chrome extension publishes ``podcast-audio`` as soon as it
        connects — typically before the agent dispatches into the room.
        Without this replay the live event has already fired and our
        handler never runs.
        """
        for participant in list(self._room.remote_participants.values()):
            for publication in list(participant.track_publications.values()):
                track = getattr(publication, "track", None)
                if track is None:
                    continue
                try:
                    self._on_track_subscribed(track, publication, participant)
                except Exception:
                    logger.exception("Replay of track_subscribed failed")

    def _on_track_subscribed(self, track: Any, publication: Any, participant: Any) -> None:
        track_name = getattr(publication, "name", "")
        identity = getattr(participant, "identity", "")
        logger.info(
            "Track subscribed [name=%s from=%s kind=%s]",
            track_name,
            identity,
            getattr(track, "kind", "?"),
        )
        if track_name == "podcast-audio":
            self._podcast.attach_track(track)

    def _on_participant_disconnected(self, participant: Any) -> None:
        """End the job when the user leaves.

        The framework auto-closes the ``AgentSession`` on participant
        disconnect (``RoomInputOptions.close_on_disconnect`` is True by
        default) but the *job* keeps running — so the silence loop and
        any pending fire-and-forget tasks would keep firing into a dead
        session. Avatar participants disconnecting is normal mid-call
        churn — only a user disconnect tears the room down.
        """
        identity = getattr(participant, "identity", "") or ""
        if identity.startswith(_AVATAR_IDENTITY_PREFIX):
            return
        if self._shutting_down or self._shutdown_task is not None:
            return
        logger.info("User participant %s disconnected — tearing down", identity)
        self._shutdown_task = asyncio.create_task(
            self._shutdown_on_user_disconnect(), name="director_user_disconnect"
        )
        self._shutdown_task.add_done_callback(_log_task_exception)

    async def _shutdown_on_user_disconnect(self) -> None:
        """Shutdown ourselves, then ask main.py to terminate the job."""
        try:
            await self.shutdown()
        finally:
            if self._on_user_disconnect is not None:
                with contextlib.suppress(Exception):
                    await self._on_user_disconnect()

    # ==================================================================
    # Inbound control handlers
    # ==================================================================
    def _handle_skip(self, _msg: dict) -> None:
        """User hit "Skip commentary" — cut off skippable turns only."""
        self._skip.request_skip()

    def _handle_settings(self, msg: dict) -> None:
        self.update_settings(frequency=msg.get("frequency"), length=msg.get("length"))

    def update_settings(self, *, frequency: str | None = None, length: str | None = None) -> None:
        """Apply a new frequency/length preference from the UI."""
        self._settings.update(frequency=frequency, length=length)

    # ==================================================================
    # Persistence
    # ==================================================================
    def _persist(self, role: str, content: str, metadata: dict | None) -> None:
        if not self._session_id or not content:
            return
        if self._shutting_down:
            return
        self._tasks.fire_and_forget(
            log_conversation_message(self._session_id, role, content, metadata),
            name=f"director.persist.{role}",
        )


# ---------------------------------------------------------------------------
# Helper for main.py to set callbacks before session.start. The Director's
# constructor already wires these; this remains for callers that replace a
# persona post-construction (and as a stable import surface).
# ---------------------------------------------------------------------------


def attach_persona_callbacks(director: Director, personas: list[PersonaAgent]) -> None:
    """Idempotent — re-binds callbacks if a persona is replaced post-construction."""
    for p in personas:
        p._on_speech_start_cb = director._on_persona_speech_start  # type: ignore[attr-defined]
        p._on_speech_end_cb = director._on_persona_speech_end  # type: ignore[attr-defined]
        p._on_turn_finalised_cb = director._on_persona_turn_finalised  # type: ignore[attr-defined]


__all__ = [
    "Director",
    "attach_persona_callbacks",
]
