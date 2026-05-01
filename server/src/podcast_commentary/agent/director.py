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
  * :class:`PlayoutWaiter`  — bounded wait on ``SpeechHandle.wait_for_playout``
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
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

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
from podcast_commentary.agent.secondary_room import SecondaryRoomConnector
from podcast_commentary.agent.selector import SpeakerSelector
from podcast_commentary.agent.settings_controller import SettingsController
from podcast_commentary.agent.skip_coordinator import SkipCoordinator
from podcast_commentary.agent.task_supervisor import TaskSupervisor
from podcast_commentary.agent.user_presence import (
    _AVATAR_IDENTITY_PREFIX,
    UserPresenceMonitor,
)
from podcast_commentary.core.config import settings
from podcast_commentary.core.db import log_conversation_message

logger = logging.getLogger("podcast-commentary.director")


# Re-exported so tests that patch ``director_module._AVATAR_IDENTITY_PREFIX``
# keep working without reaching into ``user_presence``.
__all_constants__ = (_AVATAR_IDENTITY_PREFIX,)


# Hard timeout for the heartbeat watchdog. If the user is missing
# from every room for this many seconds, the Director force-trips the
# shutdown latch even without a clean ``participant_disconnected`` event.
# Protects against tab-kill / network-pull scenarios where the SDK never
# observes the user leaving.
_DEFAULT_USER_HEARTBEAT_TIMEOUT_S = 30.0
# How often the watchdog polls room membership. The check is cheap (just
# iterating ``remote_participants``) so 1 s is fine and keeps the worst
# case for trip detection at ``timeout + poll`` ≈ 31 s.
# Read at each poll iteration via a provider so tests can monkeypatch this
# module attribute after the monitor is constructed.
_HEARTBEAT_POLL_INTERVAL_S = 1.0


class PersonaContext(NamedTuple):
    """Per-persona triple the Director consumes — one per persona in the show.

    Each persona owns its own ``rtc.Room`` and ``AgentSession``. The
    Director treats the FIRST context as the primary (user-facing) — its
    room is where the Chrome extension joins, where
    ``commentary.control`` is published, and where podcast-audio
    arrives. Per-persona lookups (e.g. avatar-readiness, future per-room
    track replay) consult the individual context's room/session.
    """

    persona: PersonaAgent
    room: rtc.Room
    session: Any


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Fire-and-forget task %r failed: %s", task.get_name(), exc, exc_info=exc)


class Director:
    """One Director per job. Lives for the whole show.

    Construction is cheap; real work begins on ``start()`` (after every
    persona's ``ready`` Event has fired). The Director takes a list of
    :class:`PersonaContext` triples — one per persona — and treats the
    *first* triple's room as the user-facing primary (control channel,
    podcast-audio listener, participant disconnect). Per-persona room
    and session lookups go through :meth:`_room_for` / :meth:`_session_for`.
    """

    def __init__(
        self,
        *,
        personas: list[PersonaContext],
        avatar_identities: dict[str, str] | None = None,
        session_id: str | None = None,
        on_user_disconnect: Callable[[], Awaitable[None]] | None = None,
        secondary_connectors: list[SecondaryRoomConnector] | None = None,
        user_heartbeat_timeout_s: float = _DEFAULT_USER_HEARTBEAT_TIMEOUT_S,
        avatar_startup_ms: dict[str, float] | None = None,
    ) -> None:
        if not personas:
            raise ValueError("Director needs at least one PersonaContext")
        # Avatar identities must be globally unique across personas.
        # Per-room uniqueness alone isn't enough: the extension routes
        # incoming tracks by matching the avatar identity prefix, and
        # log clarity benefits from each avatar being addressable by
        # name. ``_avatar_identity_for`` derives the identity from
        # persona name, so a duplicate here means two personas share a
        # name — fail loudly at startup rather than silently collapsing them.
        identities = list((avatar_identities or {}).values())
        if len(identities) != len(set(identities)):
            raise ValueError(f"Avatar identities must be globally unique; got {identities!r}")
        # Each room hosts at most one avatar + one user (dual-room
        # mode). When a participant_disconnected event fires the handler
        # only needs to know "is this our avatar?" — so store the explicit
        # known avatar identities rather than reaching for a prefix heuristic.
        self._avatar_identities: frozenset[str] = frozenset(identities)

        self._contexts = list(personas)
        self._personas = [ctx.persona for ctx in self._contexts]
        # Per-persona lookups: dual-room mode hands each persona its own
        # ``rtc.Room`` + ``AgentSession``; the Director resolves either
        # via these maps.
        self._room_by_persona: dict[str, rtc.Room] = {
            ctx.persona.name: ctx.room for ctx in self._contexts
        }
        self._session_by_persona: dict[str, Any] = {
            ctx.persona.name: ctx.session for ctx in self._contexts
        }
        # The first persona's room is the user-facing primary: it's where
        # the Chrome extension connects, the ``commentary.control`` data
        # channel is published, and the ``podcast-audio`` track arrives.
        # Single-room mode collapses to one shared room so this is also
        # the only room.
        self._primary_room: rtc.Room = self._contexts[0].room
        self._session_id = session_id
        # Called once, after shutdown, when the *user* (not an avatar)
        # leaves the room. Lets main.py terminate the job so a new call
        # dispatches into a fresh worker instead of inheriting zombie state.
        self._on_user_disconnect = on_user_disconnect

        # Foundational services with no orchestration deps.
        self._tasks = TaskSupervisor()
        self._room_state = RoomState(self._personas)
        self._timer = CommentaryTimer()
        self._full_transcript = FullTranscript()
        self._podcast = PodcastPipeline(on_transcript=self._handle_podcast_transcript)
        self._selector = SpeakerSelector(
            model=settings.DIRECTOR_LLM_MODEL,
            max_consecutive=settings.DIRECTOR_MAX_CONSECUTIVE,
        )
        self._skip = SkipCoordinator(self._personas)
        self._control = ControlChannel(self._primary_room)
        self._playout = PlayoutWaiter()

        # Orchestration components.
        self._intros = IntroSequencer(
            personas=self._personas,
            rooms=self._room_by_persona,
            avatar_identities=dict(avatar_identities or {}),
            room_state=self._room_state,
            control=self._control,
            playout_waiter=self._playout,
        )
        self._pipeline = CommentaryPipeline(
            personas=self._personas,
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
            personas=self._personas,
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
        # Set on the first ``podcast-audio`` subscription so the
        # confirmation INFO log only fires once even if track replay
        # re-delivers the event after the live one already landed.
        self._podcast_audio_subscribed: bool = False

        # ----- shutdown latch --------------------------------------------
        # Single source of truth for "the show is winding down because the
        # user left." Flipped by:
        #   - any room observing the user identity disconnect
        #   - the heartbeat watchdog (30 s with no user in any room)
        # Once set, ``_shutdown_on_latch`` runs ``aclose()`` on every
        # secondary connector in parallel and asks main.py to terminate
        # the job. This is intentionally NOT a ``try/finally`` in the
        # entrypoint: transient errors must not tear the show down — only
        # an explicit user-departure or heartbeat miss does.
        self.session_shutdown: asyncio.Event = asyncio.Event()
        self._secondary_connectors: list[SecondaryRoomConnector] = list(secondary_connectors or [])
        self._user_heartbeat_timeout_s = user_heartbeat_timeout_s
        # Owns the polling loop, the last-seen clock, and the
        # user-vs-avatar discrimination. The provider closure reads
        # ``_HEARTBEAT_POLL_INTERVAL_S`` from THIS module so test
        # monkeypatches against ``director_module._HEARTBEAT_POLL_INTERVAL_S``
        # take effect on every iteration.
        self._presence = UserPresenceMonitor(
            rooms_provider=lambda: (ctx.room for ctx in self._contexts),
            timeout_s=user_heartbeat_timeout_s,
            on_timeout=self._on_user_heartbeat_timeout,
            stop_event=self.session_shutdown,
            poll_interval_provider=lambda: _HEARTBEAT_POLL_INTERVAL_S,
        )

        # ----- session-lifecycle log state ------------------------------
        # ``_avatar_startup_ms`` is a live reference handed in by the
        # entrypoint — the avatar startup watcher (metrics.watch_avatar_startup)
        # mutates it on success, so by teardown the dict has one entry per
        # avatar that published video. ``_total_turns`` increments on every
        # finalised assistant turn (intros + commentary). ``_end_reason``
        # narrows the lifecycle log's ``end_reason`` field — only the
        # user-disconnect and heartbeat-timeout paths set it explicitly;
        # everything else (worker shutdown, exceptions) defaults to "error".
        self._avatar_startup_ms: dict[str, float] = (
            avatar_startup_ms if avatar_startup_ms is not None else {}
        )
        self._total_turns: int = 0
        self._end_reason: str | None = None
        # Captured at start() so the lifecycle log can report duration
        # without us threading a separate timer through every component.
        self._session_started_at: float | None = None

        # Mediator: each persona's events fan out through us so other
        # components don't reach into PersonaAgent's private callback attrs.
        self._wire_persona_callbacks()

    # ==================================================================
    # Per-persona room/session resolution
    # ==================================================================
    def _room_for(self, persona: PersonaAgent) -> rtc.Room:
        """Return the ``rtc.Room`` that hosts ``persona``.

        Single-room mode: every persona returns the shared room.
        Dual-room mode: each persona returns its own room (primary
        persona's room is also ``self._primary_room``).
        """
        return self._room_by_persona[persona.name]

    def _session_for(self, persona: PersonaAgent) -> Any:
        """Return the ``AgentSession`` bound to ``persona``."""
        return self._session_by_persona[persona.name]

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

        # Reset the heartbeat clock — at this point the entrypoint has
        # finished ``ctx.connect()`` so any present user is visible in
        # ``remote_participants``. Earlier timestamps from __init__ are
        # stale by however long avatar startup took.
        self._presence.last_user_seen = time.monotonic()
        self._session_started_at = time.monotonic()
        self._tasks.fire_and_forget(self._heartbeat_watchdog(), name="director_heartbeat_watchdog")

        # Intros run in the background so a slow per-persona intro
        # can't freeze the whole room. The silence loop, watchdog, and
        # commentary paths all guard on ``RoomState.is_listening()`` so
        # a pending intro keeps them quiet without keeping them
        # unstarted.
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
        # Coupling shutdown() to the latch keeps the heartbeat watchdog
        # from continuing to poll a torn-down room, and lets external
        # callers (ctx.add_shutdown_callback) flow through the same gate.
        self.session_shutdown.set()
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

        # One structured INFO line per session, after every other
        # teardown step has had a chance to update its counters. Wrapped
        # in suppress so a logging glitch can't mask a real teardown error.
        with contextlib.suppress(Exception):
            self._emit_session_lifecycle_log()

    def _emit_session_lifecycle_log(self) -> None:
        """Emit the session-lifecycle line.

        One INFO log, JSON payload after the ``session_lifecycle`` tag,
        so the Fly log shipper / log-based metrics pipeline can scrape
        the structured fields. Deliberately PII-free: no podcast title,
        no transcript content, no participant identities — only the
        per-session shape (personas, room names, turn counts, end reason)
        we need to slice retrospectives.
        """
        seen_room_ids: set[int] = set()
        room_names: list[str] = []
        for ctx in self._contexts:
            if id(ctx.room) in seen_room_ids:
                continue
            seen_room_ids.add(id(ctx.room))
            name = getattr(ctx.room, "name", None)
            if name:
                room_names.append(name)

        avatar_startup_ms = {
            persona: round(elapsed * 1000, 1)
            for persona, elapsed in self._avatar_startup_ms.items()
        }

        duration_s: float | None = None
        if self._session_started_at is not None:
            duration_s = round(time.monotonic() - self._session_started_at, 2)

        payload = {
            "session_id": self._session_id,
            "primary_persona": self._personas[0].name if self._personas else None,
            "secondary_personas": [p.name for p in self._personas[1:]],
            "room_names": room_names,
            "avatar_startup_ms": avatar_startup_ms,
            "total_turns": self._total_turns,
            "end_reason": self._end_reason or "error",
            "duration_s": duration_s,
        }
        logger.info("session_lifecycle %s", json.dumps(payload, sort_keys=True))

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
        transition never fires.
        """
        if self._shutting_down:
            return
        if self._room_state.is_listening():
            self._scheduler.rearm_silence()

    async def _on_persona_turn_finalised(
        self, persona: PersonaAgent, text: str, angle: str | None
    ) -> None:
        """Persona's assistant message landed — record it for the room."""
        self._total_turns += 1
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
        """Attach to every room owned by this Director.

        The Chrome extension always joins the primary room — that's
        where podcast-audio is published, where ``commentary.control``
        is dispatched, and where the user's ``participant_disconnected``
        event fires. Secondary rooms (dual-room mode) carry only the
        co-host's avatar tracks.

        Secondary rooms get a defense-in-depth ``track_subscribed``
        listener — they have NO subscription pathway for
        ``podcast-audio``, but if the extension ever publishes one there
        by mistake we log + drop it instead of silently ingesting audio
        from the wrong room.

        ``participant_disconnected`` is wired on EVERY room so
        any room observing the user identity disconnect trips the
        shutdown latch. The user normally only joins the primary, but
        the safety net stays cheap and catches misrouted disconnects.
        """
        self._control.attach()
        self._primary_room.on("track_subscribed", self._on_track_subscribed)

        # Single-room mode collapses every PersonaContext onto the same
        # room object, so de-dupe by identity to avoid double-attaching
        # the secondary handler to the primary.
        seen_room_ids: set[int] = {id(self._primary_room)}
        self._primary_room.on("participant_disconnected", self._on_participant_disconnected)
        for ctx in self._contexts[1:]:
            secondary = ctx.room
            if id(secondary) in seen_room_ids:
                continue
            seen_room_ids.add(id(secondary))
            secondary.on("track_subscribed", self._on_secondary_track_subscribed)
            secondary.on("participant_disconnected", self._on_participant_disconnected)

    def _replay_existing_tracks(self) -> None:
        """Replay track_subscribed for tracks present before we subscribed.

        The Chrome extension publishes ``podcast-audio`` as soon as it
        connects — typically before the agent dispatches into the room.
        Without this replay the live event has already fired and our
        handler never runs. Only the primary room carries podcast-audio.
        """
        for participant in list(self._primary_room.remote_participants.values()):
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
            if not self._podcast_audio_subscribed:
                self._podcast_audio_subscribed = True
                logger.info(
                    "podcast audio subscribed in primary room %s",
                    getattr(self._primary_room, "name", "?"),
                )
            self._podcast.attach_track(track)

    def _on_secondary_track_subscribed(
        self, track: Any, publication: Any, participant: Any
    ) -> None:
        """Defense-in-depth: secondary rooms must never carry podcast-audio.

        The Chrome extension only publishes ``podcast-audio`` to the
        primary room. Secondary rooms exist solely to host one co-host
        avatar's video. If a ``podcast-audio`` track ever appears here
        — misconfigured client, future regression — log a warning and
        drop the track on the floor rather than wiring it into the STT
        pipeline behind the primary listener's back.
        """
        track_name = getattr(publication, "name", "")
        if track_name != "podcast-audio":
            return
        logger.warning(
            "podcast-audio appeared on a secondary room — ignoring "
            "[from=%s room=%s] (extension should only publish to the primary)",
            getattr(participant, "identity", ""),
            getattr(getattr(track, "_room", None), "name", "?"),
        )

    def _on_participant_disconnected(self, participant: Any) -> None:
        """Trip the shutdown latch when the user leaves any room.

        The framework auto-closes the ``AgentSession`` on participant
        disconnect (``RoomInputOptions.close_on_disconnect`` is True by
        default) but the *job* keeps running — so the silence loop and
        any pending fire-and-forget tasks would keep firing into a dead
        session.

        Avatar participants disconnecting is normal mid-call churn
        (LemonSlice rendering pod restart) — log INFO and let the
        SDK reconnect the avatar. Anything else IS the user.

        Dual-room mode hosts at most one avatar + one user per
        room, so the disconnecting identity is either the room's known
        avatar (transient) or the user (latch-tripping) — no prefix
        heuristic needed.
        """
        identity = getattr(participant, "identity", "") or ""
        if identity in self._avatar_identities:
            logger.info(
                "Avatar %s disconnected — letting LemonSlice reconnect, latch unaffected",
                identity,
            )
            return
        if self.session_shutdown.is_set():
            return
        logger.info("User participant %s disconnected — tripping shutdown latch", identity)
        if self._end_reason is None:
            self._end_reason = "user_disconnect"
        self._trip_shutdown_latch()

    def _trip_shutdown_latch(self) -> None:
        """Idempotent: set the latch and spawn the teardown task once.

        Intentionally uses a bare ``asyncio.create_task`` rather than
        ``TaskSupervisor.fire_and_forget`` — ``_shutdown_on_latch``
        cancels every supervised task as part of teardown, so tracking
        it in the same supervisor would have it cancel itself partway
        through. The handle is parked on ``self._shutdown_task`` so
        callers (and tests) can await teardown completion.
        """
        if self.session_shutdown.is_set() and self._shutdown_task is not None:
            return
        self.session_shutdown.set()
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown_on_latch(), name="director_shutdown_latch"
            )
            self._shutdown_task.add_done_callback(_log_task_exception)

    async def _shutdown_on_latch(self) -> None:
        """Latch-driven teardown.

        Order:
          1. Close every secondary connector in parallel — stops LemonSlice
             billing and avoids orphaned avatars.
          2. ``shutdown()`` ourselves — cancels the silence loop, intros,
             and any persisting tasks.
          3. ``on_user_disconnect`` — main.py terminates the job so the
             next call dispatches into a fresh worker.
        """
        if self._secondary_connectors:
            await asyncio.gather(
                *(c.aclose() for c in self._secondary_connectors),
                return_exceptions=True,
            )
        try:
            await self.shutdown()
        finally:
            if self._on_user_disconnect is not None:
                with contextlib.suppress(Exception):
                    await self._on_user_disconnect()

    async def _heartbeat_watchdog(self) -> None:
        """Drive the UserPresenceMonitor until it trips or shutdown completes.

        Kept on Director (rather than calling the monitor directly) so
        ``ctx.add_shutdown_callback`` paths and tests have a stable
        method on the Director surface to spawn the watchdog from.
        """
        await self._presence.run()

    def _on_user_heartbeat_timeout(self) -> None:
        """Monitor callback: user was absent past the timeout window."""
        if self._end_reason is None:
            self._end_reason = "timeout"
        self._trip_shutdown_latch()

    # ``_last_user_seen`` and ``_user_present_in_any_room`` are kept as
    # delegations for tests that drive the watchdog directly: they reset
    # the clock or assert presence at specific moments.
    @property
    def _last_user_seen(self) -> float:
        return self._presence.last_user_seen

    @_last_user_seen.setter
    def _last_user_seen(self, value: float) -> None:
        self._presence.last_user_seen = value

    def _user_present_in_any_room(self) -> bool:
        return self._presence.is_user_present()

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
    "PersonaContext",
    "attach_persona_callbacks",
]
