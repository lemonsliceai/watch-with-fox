"""Fox — the commentary Agent.

`ComedianAgent` is the composition root. It owns:

  * `SpeechGate` — authoritative "is Fox speaking?" gate + `speak()`
  * `UserTurnTracker` — hold-to-talk state machine
  * `PodcastPipeline` — podcast STT stream + LiveKit track consumer
  * `CommentaryTimer` — MIN_GAP / burst rules between turns
  * `FullTranscript` — rolling podcast transcript

…and the behaviour that *coordinates* them: when a podcast line lands, check
the gates and deliver a reaction; when the podcast goes quiet, step in with a
reflective beat; when the user hits push-to-talk, interrupt Fox and
answer them. Prompt-assembly and persistence are inlined here because they're
thin enough that extracting them would cost more than it saves.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import random
from collections.abc import AsyncIterable
from typing import Any

from livekit.agents import Agent, ModelSettings, llm
from livekit.rtc._proto.track_pb2 import TrackSource

from podcast_commentary.agent.commentary import (
    CommentaryTimer,
    FullTranscript,
    SENTENCE_THRESHOLD,
)
from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.podcast_pipeline import PodcastPipeline
from podcast_commentary.agent.prompts import (
    SAMPLING_SENTINEL,
    build_commentary_request,
    build_user_reply_request,
    pick_angle,
)
from podcast_commentary.agent.speech_gate import SpeechGate
from podcast_commentary.agent.user_turn import UserTurnTracker
from podcast_commentary.core.db import log_conversation_message

logger = logging.getLogger("podcast-commentary.agent")


def _fire_and_forget(coro: Any, *, name: str = "") -> asyncio.Task:
    """Schedule a coroutine without awaiting it, but log any exception.

    Bare ``asyncio.create_task()`` silently swallows exceptions (the Task
    holds them until GC, which may never log). This wrapper attaches a
    done-callback that surfaces failures immediately.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Fire-and-forget task %r failed: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )


# Timing + playout knobs — sourced from the active FoxConfig preset.
# See fox_configs/default.py for descriptions.
POST_SPEECH_DELAY = CONFIG.timing.post_speech_safety_s
SILENCE_FALLBACK_DELAY = CONFIG.timing.silence_fallback_s
INTRO_PLAYOUT_TIMEOUT = CONFIG.playout.intro_timeout_s
COMMENTARY_PLAYOUT_TIMEOUT = CONFIG.playout.commentary_timeout_s


# ---------------------------------------------------------------------------
# Verbalized-sampling helpers — persona-neutral: never assume "joke"/"line"
# semantics, just route a string picked by the configured strategy.
# ---------------------------------------------------------------------------


def _prompt_uses_sampling(chat_ctx: llm.ChatContext) -> bool:
    """True when the most recent user message carries the sampling sentinel."""
    for item in reversed(chat_ctx.items):
        if not isinstance(item, llm.ChatMessage) or item.role != "user":
            continue
        text = item.text_content or ""
        return SAMPLING_SENTINEL in text
    return False


def _chunk_text(chunk: Any) -> str:
    """Extract text from a streaming LLM chunk (``str`` or ``ChatChunk``)."""
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, llm.ChatChunk) and chunk.delta and chunk.delta.content:
        return chunk.delta.content
    return ""


def _select_candidate(raw: str, strategy: str) -> str:
    """Parse the VS JSON envelope and pick one candidate's ``line``.

    Falls back to the raw text if anything about the JSON is wrong — better
    a slightly-malformed line reaches TTS than a silent turn.
    """
    payload = raw.strip()
    # Models occasionally wrap JSON in ```json fences despite instructions.
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
        payload = payload.strip()

    try:
        data = json.loads(payload)
        candidates = data["candidates"]
        if not candidates:
            raise ValueError("empty candidates array")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("VS parse failed (%s) — falling back to raw text", exc)
        return raw.strip()

    # Coerce probabilities; missing/invalid → 0 so they sort last.
    for c in candidates:
        try:
            c["p"] = float(c.get("p", 0.0))
        except (TypeError, ValueError):
            c["p"] = 0.0

    if strategy == "top_k_random":
        top = sorted(candidates, key=lambda c: c["p"], reverse=True)[:3]
        winner = random.choice(top)
    else:  # "max_prob" and any unknown value
        winner = max(candidates, key=lambda c: c["p"])

    line = (winner.get("line") or "").strip()
    if not line:
        logger.warning("VS winner had empty line — falling back to raw text")
        return raw.strip()

    logger.info(
        "VS picked candidate (strategy=%s, p=%.2f, of %d): %s",
        strategy,
        winner["p"],
        len(candidates),
        line[:120],
    )
    return line


class FoxPhase(enum.Enum):
    """Explicit lifecycle phases for Fox's commentary session.

    State machine::

        ┌─────────┐
        │  INTRO  │──────────────────────────┐
        └────┬────┘                          │
             │ speech done                   │ user_talk_start
             ▼                               ▼
        ┌───────────┐  transcript/silence  ┌──────────────┐
        │ LISTENING │─────────────────────►│ COMMENTATING │
        │           │◄─────────────────────│              │
        └─────┬─────┘  speech done         └──────┬───────┘
              │                                   │
              │ user_talk_start                   │ user_talk_start
              ▼                                   ▼
        ┌──────────────┐                   (interrupt + ─►)
        │ USER_TALKING │◄─────────────────────────┘
        └──────┬───────┘
               │ grace timer + committed text
               ▼
        ┌──────────┐
        │ REPLYING │───► LISTENING (speech done)
        └──────────┘

    Every phase is entered synchronously (before any ``await``) so there
    is never an ambiguous window where a racing event sees stale state.
    """

    INTRO = "intro"
    LISTENING = "listening"
    COMMENTATING = "commentating"
    USER_TALKING = "user_talking"
    REPLYING = "replying"


# Legal transitions — any transition not listed here is a bug.
_VALID_TRANSITIONS: dict[FoxPhase, set[FoxPhase]] = {
    FoxPhase.INTRO: {FoxPhase.LISTENING, FoxPhase.USER_TALKING},
    FoxPhase.LISTENING: {FoxPhase.COMMENTATING, FoxPhase.USER_TALKING, FoxPhase.INTRO},
    FoxPhase.COMMENTATING: {FoxPhase.LISTENING, FoxPhase.USER_TALKING},
    FoxPhase.USER_TALKING: {FoxPhase.REPLYING, FoxPhase.LISTENING},
    FoxPhase.REPLYING: {FoxPhase.LISTENING, FoxPhase.USER_TALKING},
}


class ComedianAgent(Agent):
    """Fox — the AI comedian who watches podcasts with you."""

    def __init__(
        self,
        instructions: str,
        *,
        session_id: str | None = None,
    ) -> None:
        super().__init__(instructions=instructions)
        # Conversation state — shared across producers.
        self._timer = CommentaryTimer()
        self._full_transcript = FullTranscript()
        self._commentary_history: list[str] = []
        # Rotated comedic "angles" so successive comments don't collapse into
        # one house voice. `pick_angle` excludes the last few used names.
        self._recent_angles: list[str] = []
        # Angle chosen for the *in-flight* turn. Stashed here so the
        # `conversation_item_added` hook (which doesn't know what the caller
        # picked) can append the right label to `_recent_angles`.
        self._pending_angle_name: str | None = None

        # Persistence — every utterance, reply, and summary flows into the
        # conversation_messages table keyed on this session_id. If the API
        # server didn't supply one, persistence silently no-ops.
        self._session_id = session_id

        # Collaborators — initialised in `on_enter` once `self.session` is
        # real. Before that, the underlying AgentSession doesn't exist yet.
        self._gate: SpeechGate | None = None
        self._user_turn: UserTurnTracker | None = None
        self._podcast: PodcastPipeline | None = None

        # Explicit phase — the single source of truth for "what is Fox
        # doing right now?" Replaces the implicit combination of
        # gate.is_speaking + user_turn.talking + timer.can_comment().
        self._phase = FoxPhase.LISTENING

        # Background tasks owned by this agent.
        self._commentary_delay_task: asyncio.Task | None = None

    # ==================================================================
    # LLM node override — verbalized sampling (persona-neutral)
    # ==================================================================
    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str]:
        """Buffer + select when the prompt opted into verbalized sampling.

        Detection is by ``SAMPLING_SENTINEL`` in the latest user message —
        the prompt builders only emit it when ``CONFIG.sampling.num_candidates
        > 1``. When absent, we delegate to the framework default and stream
        normally. When present, we buffer the full JSON, pick one candidate
        per ``CONFIG.sampling.selection``, and yield the winner as a single
        chunk (TTS plays it as one utterance).

        Persona-neutral: this code never says "joke" — it just routes a
        line picked by the configured strategy. Each preset decides whether
        VS is on and which selection rule to use.
        """
        default_node = Agent.default.llm_node(self, chat_ctx, tools, model_settings)

        if CONFIG.sampling.num_candidates <= 1 or not _prompt_uses_sampling(chat_ctx):
            async for chunk in default_node:
                yield chunk
            return

        buf: list[str] = []
        async for chunk in default_node:
            text = _chunk_text(chunk)
            if text:
                buf.append(text)
        raw = "".join(buf)

        winner = _select_candidate(raw, CONFIG.sampling.selection)
        yield winner

    # ==================================================================
    # Public state (read by collaborators and tests)
    # ==================================================================
    @property
    def phase(self) -> FoxPhase:
        return self._phase

    @property
    def is_speaking(self) -> bool:
        """Delegates to `SpeechGate` once composed; False before entry."""
        return self._gate is not None and self._gate.is_speaking

    def _set_phase(self, new: FoxPhase) -> None:
        old = self._phase
        if old is new:
            return
        valid = _VALID_TRANSITIONS.get(old, set())
        if new not in valid:
            logger.error(
                "Illegal phase transition: %s → %s (allowed: %s)",
                old.value,
                new.value,
                {v.value for v in valid},
            )
            return
        self._phase = new
        logger.info("Phase: %s → %s", old.value, new.value)
        if new == FoxPhase.LISTENING:
            self._schedule_next_commentary()
            self._check_sentence_threshold()

    # ==================================================================
    # Lifecycle
    # ==================================================================
    async def on_enter(self) -> None:
        """Compose collaborators, wire listeners, and speak the intro.

        Ordering is load-bearing: every synchronous setup step runs before
        the first `await`, and the intro `speak()` call happens *before*
        any `publish_data` awaits. Because `speak()` assigns the speech
        handle synchronously, `is_speaking` flips True immediately — so any
        podcast transcript that lands during the awaits below sees the gate
        closed and correctly skips firing a duplicate commentary.
        """
        self._compose_collaborators()
        self._register_listeners()
        self._log_existing_participants()
        self._start_podcast_pipeline()
        # Must come AFTER pipeline init — the replay may attach the
        # podcast-audio track to it immediately.
        self._replay_existing_tracks()

        logger.info("Fox entering session — sending intro")

        # SYNCHRONOUS: closes the gate before any await below.
        self._speak_intro()

        # First awaits — the gate is already closed, so racing transcripts
        # are safely dropped by `_handle_podcast_transcript`.
        await self._publish_agent_ready()
        await self._publish_commentary_start()

    async def shutdown(self) -> None:
        """Tear down the podcast pipeline and cancel timers on agent shutdown."""
        if self._commentary_delay_task is not None:
            self._commentary_delay_task.cancel()
        if self._podcast is not None:
            await self._podcast.shutdown()

    # ------------------------------------------------------------------
    # on_enter helpers
    # ------------------------------------------------------------------
    def _compose_collaborators(self) -> None:
        """Instantiate SpeechGate, UserTurnTracker, PodcastPipeline."""
        self._gate = SpeechGate(self.session, on_released=self._on_speech_released)
        self._user_turn = UserTurnTracker(
            session=self.session,
            on_committed=self._handle_user_committed,
            on_start=self._on_user_talk_start,
            on_empty=lambda: self._set_phase(FoxPhase.LISTENING),
        )
        # Chrome extension captures tab audio and publishes it as a LiveKit
        # track named ``podcast-audio``. The pipeline subscribes to that
        # track to feed STT.
        self._podcast = PodcastPipeline(on_transcript=self._handle_podcast_transcript)

    def _register_listeners(self) -> None:
        """Wire room + session events to handler methods."""
        room = self.session.room_io.room
        room.on("data_received", self._on_data_received)
        room.on("track_subscribed", self._log_track_subscribed)
        room.on("track_published", self._log_track_published)

        self.session.on("user_input_transcribed", self._on_stt_transcribed)
        # `conversation_item_added` gives us Fox's finalised lines for
        # history/angle/persistence. Drives rotation so successive
        # commentaries don't collapse into one voice.
        self.session.on("conversation_item_added", self._on_conversation_item_added)
        # `agent_state_changed` drives ONLY the CommentaryTimer (real audio
        # start/end). It does NOT gate `is_speaking` — that gate reads
        # `SpeechHandle.done()`, which is authoritative.
        self.session.on("agent_state_changed", self._on_agent_state_changed)

    def _log_existing_participants(self) -> None:
        """Diagnostic: who's already in the room when the agent joins?

        By design the browser joins first (its YouTube iframe has been
        playing for a few seconds before dispatch). If no remote
        participants are visible here, something is wrong with job
        dispatch.
        """
        room = self.session.room_io.room
        try:
            remote = list(getattr(room, "remote_participants", {}).values())
            logger.info(
                "Agent joining room with %d existing remote participant(s): %s",
                len(remote),
                [getattr(p, "identity", "?") for p in remote],
            )
        except Exception:
            logger.debug("Could not enumerate remote_participants", exc_info=True)

    def _start_podcast_pipeline(self) -> None:
        """Start the STT loop; podcast audio arrives via the extension's LiveKit track."""
        assert self._podcast is not None
        self._podcast.start()

    def _replay_existing_tracks(self) -> None:
        """Replay track_subscribed for tracks that were subscribed before
        our handler was registered.

        The Chrome extension publishes the ``podcast-audio`` track as soon
        as its ``room.connect()`` resolves, which is typically BEFORE the
        agent has been dispatched into the room. When the agent later
        joins with ``AUTO_SUBSCRIBE_ALL``, LiveKit subscribes to the
        pre-existing track immediately — but that ``track_subscribed``
        event fires before ``on_enter`` runs and therefore before
        ``_register_listeners`` hooks our handler. Without this replay,
        the track is silently ignored and the STT pipeline never gets any
        podcast audio.

        Tracks published AFTER ``on_enter`` (e.g. the LemonSlice avatar
        tracks) are handled normally via the live event; this replay only
        matters for pre-existing subscriptions.
        """
        room = self.session.room_io.room
        try:
            participants = list(getattr(room, "remote_participants", {}).values())
        except Exception:
            logger.debug("Could not enumerate remote_participants for replay", exc_info=True)
            return

        for participant in participants:
            pubs = list(getattr(participant, "track_publications", {}).values())
            for publication in pubs:
                track = getattr(publication, "track", None)
                if track is None:
                    continue
                logger.info(
                    "Replaying pre-existing track_subscribed: name=%s from=%s",
                    getattr(publication, "name", ""),
                    getattr(participant, "identity", "?"),
                )
                try:
                    self._log_track_subscribed(track, publication, participant)
                except Exception:
                    logger.exception("Replay of track_subscribed failed")

    def _speak_intro(self) -> None:
        """Kick off Fox's intro line.

        Synchronous so the gate closes before any later `await`. No angle
        is stashed — the intro isn't part of the commentary rotation.

        A background task watches the speech handle with a timeout so the
        agent transitions to LISTENING even if the avatar never acks
        playout (see ``INTRO_PLAYOUT_TIMEOUT``).
        """
        assert self._gate is not None
        self._set_phase(FoxPhase.INTRO)
        handle = self._gate.speak(prompt=CONFIG.persona.intro_prompt)
        _fire_and_forget(self._await_intro_playout(handle), name="intro_playout")

    async def _await_intro_playout(self, handle: Any) -> None:
        """Wait for the intro to finish, with a timeout safety net.

        If ``DataStreamAudioOutput.wait_for_playout`` hangs (avatar never
        sends ``lk.playback_finished``), the ``SpeechHandle`` stays
        unresolved and the phase is stuck at INTRO forever.  This task
        forces the transition so podcast commentary can begin.
        """
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=INTRO_PLAYOUT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "Intro playout timed out after %.0fs — forcing INTRO → LISTENING",
                INTRO_PLAYOUT_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Intro wait_for_playout raised", exc_info=True)

        if self._phase == FoxPhase.INTRO:
            self._set_phase(FoxPhase.LISTENING)

    async def _publish_agent_ready(self) -> None:
        """Signal the client that the agent has joined and is listening.

        The Chrome extension uses this to flip its UI mood to "Listening".
        """
        try:
            await self._publish_control({"type": "agent_ready"})
            logger.info("Sent agent_ready to client")
        except Exception:
            logger.warning("Failed to publish agent_ready", exc_info=True)

    # ------------------------------------------------------------------
    # Phase transition callbacks
    # ------------------------------------------------------------------
    def _on_user_talk_start(self) -> None:
        """User pressed push-to-talk — interrupt Fox and enter talk phase."""
        assert self._gate is not None
        self._gate.interrupt()
        self._set_phase(FoxPhase.USER_TALKING)

    def _on_speech_released(self) -> None:
        """Speech gate released — return to LISTENING unless user is mid-turn.

        The gate's identity check ensures this only fires for the *current*
        handle. If the user interrupted Fox and a new reply handle is
        already in flight, the old handle's done callback is a no-op at the
        gate level and this method is never called for it.
        """
        if self._user_turn and self._user_turn.talking:
            self._set_phase(FoxPhase.USER_TALKING)
        else:
            self._set_phase(FoxPhase.LISTENING)

    # ==================================================================
    # Data channel routing
    # ==================================================================
    def _on_data_received(self, data_packet: Any) -> None:
        """Route client messages to the right collaborator.

        Topics handled:
          - `user.control` — hold-to-talk start/end
        """
        msg = self._parse_data_packet(data_packet)
        if msg is None:
            return
        msg_type = msg.get("type")

        handlers = {
            "user_talk_start": lambda: self._user_turn and self._user_turn.start(),
            "user_talk_end": lambda: self._user_turn and self._user_turn.end(),
        }
        handler = handlers.get(msg_type)
        if handler is not None:
            handler()

    @staticmethod
    def _parse_data_packet(data_packet: Any) -> dict | None:
        """Decode JSON out of the LiveKit data packet; log + drop on failure."""
        topic = getattr(data_packet, "topic", None)
        sender = getattr(data_packet, "participant", None)
        sender_id = getattr(sender, "identity", None) if sender else None
        raw = getattr(data_packet, "data", b"")

        try:
            msg = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            size = len(raw) if isinstance(raw, (bytes, bytearray)) else -1
            logger.info(
                "Data packet [topic=%s from=%s bytes=%d] not JSON — dropping",
                topic,
                sender_id,
                size,
            )
            return None

        logger.info(
            "Data packet [topic=%s from=%s type=%s]",
            topic,
            sender_id,
            msg.get("type"),
        )
        return msg

    # ==================================================================
    # Podcast transcript → commentary
    # ==================================================================
    async def _handle_podcast_transcript(self, text: str) -> None:
        """Called by PodcastPipeline for every podcast FINAL_TRANSCRIPT.

        Every finalised line is persisted and added to the running
        transcript. If accumulated sentences meet the threshold and
        Fox is LISTENING with gates open, trigger commentary immediately.
        """
        self._persist("podcast", text)
        sentence_count = self._full_transcript.add(text)

        if (
            sentence_count >= SENTENCE_THRESHOLD
            and self._phase == FoxPhase.LISTENING
            and self._timer.can_comment()
        ):
            if self._commentary_delay_task is not None:
                self._commentary_delay_task.cancel()
            _fire_and_forget(
                self._deliver_commentary(
                    trigger_reason="react to the latest transcript",
                    energy_level="amused",
                ),
                name="sentence_trigger_commentary",
            )

    # ------------------------------------------------------------------
    # Timer-based commentary cadence
    # ------------------------------------------------------------------
    def _schedule_next_commentary(self) -> None:
        """Schedule a silence-fallback commentary after SILENCE_FALLBACK_DELAY.

        Called every time phase transitions to LISTENING. Cancels any
        existing timer so we don't stack up multiple pending commentaries.
        The primary trigger is sentence-count in _handle_podcast_transcript;
        this is the fallback for when the podcast goes quiet.
        """
        if self._commentary_delay_task is not None:
            self._commentary_delay_task.cancel()
        self._commentary_delay_task = _fire_and_forget(
            self._commentary_after_delay(), name="commentary_delay"
        )

    async def _commentary_after_delay(self) -> None:
        """Silence fallback: wait SILENCE_FALLBACK_DELAY, then deliver."""
        await asyncio.sleep(SILENCE_FALLBACK_DELAY)

        if self._phase != FoxPhase.LISTENING:
            return

        if not self._full_transcript.has_content():
            # No transcript yet — reschedule so we retry after the next delay.
            self._schedule_next_commentary()
            return

        await self._deliver_commentary(
            trigger_reason="the video has gone quiet — react to what was said",
            energy_level="amused",
        )

    def _check_sentence_threshold(self) -> None:
        """If enough sentences accumulated while Fox was speaking, trigger.

        Called on every LISTENING entry. Uses _fire_and_forget so the
        trigger runs asynchronously — _set_phase is synchronous and must
        not await.
        """
        if (
            self._full_transcript.sentences_since_reset >= SENTENCE_THRESHOLD
            and self._timer.can_comment()
        ):
            if self._commentary_delay_task is not None:
                self._commentary_delay_task.cancel()
            _fire_and_forget(
                self._deliver_commentary(
                    trigger_reason="react to the latest transcript",
                    energy_level="amused",
                ),
                name="sentence_threshold_commentary",
            )

    async def _deliver_commentary(self, *, trigger_reason: str, energy_level: str) -> None:
        """Generate and deliver a commentary line.

        Gates on phase before AND after the ducking await — a user
        push-to-talk could change phase between the two. After `speak()`
        the phase is COMMENTATING, so the silence loop / podcast consumer
        can't re-fire until the handle resolves and phase returns to
        LISTENING.
        """
        if self._phase != FoxPhase.LISTENING:
            return

        await self._publish_commentary_start()

        if self._phase != FoxPhase.LISTENING:  # may have changed during the await
            return

        self._set_phase(FoxPhase.COMMENTATING)

        prompt, angle_name = self._build_commentary_prompt(
            trigger_reason=trigger_reason, energy_level=energy_level
        )
        # Reset AFTER building the prompt so recent_transcript() still has content.
        self._full_transcript.reset_sentence_count()
        logger.info(
            "Generating commentary (trigger=%s, angle=%s, stats=%s)",
            trigger_reason,
            angle_name,
            self._timer.stats(),
        )

        assert self._gate is not None
        handle = self._gate.speak(prompt=prompt)
        try:
            await asyncio.wait_for(
                handle.wait_for_playout(),
                timeout=COMMENTARY_PLAYOUT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Commentary playout timed out after %.0fs — returning to LISTENING",
                COMMENTARY_PLAYOUT_TIMEOUT,
            )
            # Prevent back-to-back stacking: reset accumulated sentences so
            # _check_sentence_threshold() (called on LISTENING entry) doesn't
            # immediately re-trigger, and record speech end so can_comment()
            # enforces MIN_GAP from this moment — not from the previous
            # comment's real playback_finished (which could be 30s stale).
            self._full_transcript.reset_sentence_count()
            self._timer.record_speech_end()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("wait_for_playout raised — continuing", exc_info=True)

        # Ensure we return to LISTENING even if the SpeechGate callback
        # hasn't fired yet (avatar playout hang).  _set_phase no-ops if
        # we're already LISTENING (normal case where the gate callback
        # already transitioned us).
        if self._phase == FoxPhase.COMMENTATING:
            self._set_phase(FoxPhase.LISTENING)

    def _build_commentary_prompt(
        self, *, trigger_reason: str, energy_level: str
    ) -> tuple[str, str]:
        """Assemble the per-turn commentary prompt.

        Returns `(prompt_text, angle_name)`. Stashes the angle name on
        `self` so `_on_conversation_item_added` can log it with the turn.
        """
        angle = pick_angle(self._recent_angles)
        self._pending_angle_name = angle
        prompt = build_commentary_request(
            recent_transcript=self._full_transcript.recent_transcript(),
            commentary_history=self._commentary_history,
            trigger_reason=trigger_reason,
            energy_level=energy_level,
            angle=angle,
        )
        return prompt, angle

    # ==================================================================
    # User push-to-talk → reply
    # ==================================================================
    async def _handle_user_committed(self, user_text: str) -> None:
        """Called by UserTurnTracker when the user's turn is committed."""
        self._persist("user", user_text)
        await self._deliver_user_reply(user_text)

    async def _deliver_user_reply(self, user_text: str) -> None:
        """Generate a direct reply to the user.

        None of the commentary gates apply here — user speech always gets
        a response. If Fox was mid-turn, `user_turn.start` already
        interrupted him via `SpeechGate.interrupt`, so by the time we reach
        this the avatar is either silent or about to be.

        The reply is marked `allow_interruptions=True` so the user can cut
        Fox off with a fresh hold-to-talk if they want to change tack.
        """
        await self._publish_commentary_start()

        prompt, angle_name = self._build_user_reply_prompt(user_text)
        logger.info("Generating reply to user speech (angle=%s)", angle_name)

        self._set_phase(FoxPhase.REPLYING)
        assert self._gate is not None
        self._gate.speak(prompt=prompt, allow_interruptions=True)

    def _build_user_reply_prompt(self, user_text: str) -> tuple[str, str]:
        """Assemble the per-turn user-reply prompt."""
        angle = pick_angle(self._recent_angles)
        self._pending_angle_name = angle
        prompt = build_user_reply_request(
            user_text=user_text,
            recent_transcript=self._full_transcript.recent_transcript(),
            commentary_history=self._commentary_history,
            angle=angle,
        )
        return prompt, angle

    async def on_user_turn_completed(self, turn_ctx, *, new_message) -> None:
        """Fallback capture for hold-to-talk transcripts.

        Primary path: `UserTurnTracker._grace_and_commit` explicitly
        commits the user turn — that flow does NOT invoke this hook
        (`skip_reply=True` short-circuits it in the framework).

        This hook only fires if the turn detector naturally decides
        end-of-turn *during* the talk window. The transcript is buffered
        and used as a fallback if `commit_user_turn` returned empty.
        """
        text = (new_message.text_content if new_message else None) or ""
        user_talking = self._user_turn.talking if self._user_turn else False
        logger.info(
            "on_user_turn_completed [user_talking=%s, text_len=%d]: %r",
            user_talking,
            len(text),
            text[:150],
        )
        if self._user_turn is not None:
            self._user_turn.buffer(text)

    # ==================================================================
    # Event handlers — timer, angle bookkeeping, diagnostics
    # ==================================================================
    def _on_agent_state_changed(self, ev: Any) -> None:
        """Drive the commentary timer from real audio-pipeline transitions.

        `new_state == "speaking"` means audio frames are actually hitting
        the avatar (LemonSlice via the DataStreamAudioOutput RPC).
        Transitioning *away* from speaking means the avatar RPC'd
        `lk.playback_finished` back.

        This handler does NOT manage `is_speaking` — that gate is derived
        from `SpeechHandle.done()`. Here we only record real audio events
        for the CommentaryTimer and un-duck the client at end-of-speech.
        """
        logger.info(
            "Agent state: %s -> %s (phase=%s, is_speaking=%s)",
            ev.old_state,
            ev.new_state,
            self._phase.value,
            self.is_speaking,
        )
        started = ev.new_state == "speaking" and ev.old_state != "speaking"
        # Only `speaking → listening` is a true end-of-speech.
        # `speaking → thinking` means a new generation preempted the old one
        # (Fox isn't actually quiet), so we must NOT record speech end, send
        # commentary_end, or transition the phase.
        finished = ev.old_state == "speaking" and ev.new_state == "listening"
        if started:
            self._timer.record_speech_start()
        if finished:
            self._timer.record_speech_end()
            _fire_and_forget(self._publish_commentary_end(), name="commentary_end")
            # Belt-and-suspenders: if the SpeechGate's done callback hasn't
            # fired yet (avatar playout hang), transition the phase here so
            # commentary isn't blocked.  _on_speech_released is idempotent
            # via _set_phase's same-state guard.
            if self._phase in (FoxPhase.INTRO, FoxPhase.COMMENTATING, FoxPhase.REPLYING):
                self._on_speech_released()
            elif self._phase == FoxPhase.LISTENING:
                # Phase already LISTENING (e.g. playout timeout fired early).
                # Restart the delay so the 7 s counts from the REAL end of
                # speech, not from the moment the timeout forced the
                # transition.
                self._schedule_next_commentary()

    def _on_conversation_item_added(self, ev: Any) -> None:
        """Capture finalised assistant messages for history/angle/persistence.

        Fires for both roles; we only care about Fox's assistant turns.
        """
        agent_text = self._extract_assistant_text(ev)
        if agent_text is None:
            return

        self._record_commentary(agent_text)
        self._rotate_angle()
        self._flush_chat_context()

    @staticmethod
    def _extract_assistant_text(ev: Any) -> str | None:
        """Return the assistant's text from a conversation event, or None."""
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "type", None) != "message":
            return None
        if getattr(item, "role", None) != "assistant":
            return None
        text = (getattr(item, "text_content", None) or "").strip()
        return text or None

    def _record_commentary(self, agent_text: str) -> None:
        """Append to capped history and persist the agent turn."""
        logger.info("=== FOX SAID ===\n%s\n=== END FOX SAID ===", agent_text)
        self._commentary_history.append(agent_text)
        self._commentary_history = self._commentary_history[-CONFIG.context.comment_memory_size :]

        meta = {"angle": self._pending_angle_name} if self._pending_angle_name else None
        self._persist("agent", agent_text, meta)

    def _rotate_angle(self) -> None:
        """Record the used angle so ``pick_angle`` avoids it next time."""
        if self._pending_angle_name:
            self._recent_angles.append(self._pending_angle_name)
            self._recent_angles = self._recent_angles[-CONFIG.persona.angle_lookback :]
        self._pending_angle_name = None

    def _flush_chat_context(self) -> None:
        """Reset the agent's persistent chat context after each turn.

        Every ``SpeechGate.speak()`` passes a fresh ``ChatContext.empty()``
        so the LLM only sees ``[SYSTEM, USER]`` per turn — but the
        framework *also* records each ``user_input`` and assistant reply
        into ``self._chat_ctx``. Over a session this accumulates fake
        "user" messages containing full podcast transcripts, which pollutes
        the turn detector's EOT model and any framework-initiated
        auto-reply. Resetting here keeps both surfaces clean.
        """
        self._chat_ctx = llm.ChatContext.empty()

    def _on_stt_transcribed(self, ev: Any) -> None:
        """Log every STT transcription event for audio debugging."""
        logger.info(
            "STT transcription [final=%s]: %s",
            ev.is_final,
            ev.transcript[:120] if ev.transcript else "(empty)",
        )

    # ------------------------------------------------------------------
    # Track diagnostics — confirm the user's mic + avatar audio subscribe.
    # ------------------------------------------------------------------
    @staticmethod
    def _src_name(pub: Any) -> str:
        try:
            return TrackSource.Name(getattr(pub, "source", 0))
        except Exception:
            return str(getattr(pub, "source", "?"))

    def _log_track_subscribed(self, track: Any, publication: Any, participant: Any) -> None:
        track_name = getattr(publication, "name", "")
        logger.info(
            "Track subscribed [kind=%s source=%s sid=%s name=%s from=%s]",
            getattr(track, "kind", "?"),
            self._src_name(publication),
            getattr(publication, "sid", "?"),
            track_name,
            getattr(participant, "identity", "?"),
        )

        # The Chrome extension publishes a track named "podcast-audio"
        # containing the captured tab audio. Attach it to the podcast
        # pipeline so STT receives the audio directly.
        if track_name == "podcast-audio" and self._podcast is not None:
            self._podcast.attach_track(track)
            logger.info("Attached podcast-audio track to STT pipeline")

    def _log_track_published(self, publication: Any, participant: Any) -> None:
        logger.info(
            "Track published [kind=%s source=%s sid=%s from=%s]",
            getattr(publication, "kind", "?"),
            self._src_name(publication),
            getattr(publication, "sid", "?"),
            getattr(participant, "identity", "?"),
        )

    # ==================================================================
    # Client control-channel signalling
    # ==================================================================
    async def _publish_commentary_start(self) -> None:
        """Tell the client to duck video audio — Fox is about to speak."""
        try:
            await self._publish_control({"type": "commentary_start"})
        except Exception:
            logger.warning("Failed to send commentary_start signal", exc_info=True)

    async def _publish_commentary_end(self) -> None:
        """Tell the client to un-duck — Fox is done speaking."""
        try:
            await self._publish_control({"type": "commentary_end"})
        except Exception:
            logger.warning("Failed to send commentary_end signal", exc_info=True)

    async def _publish_control(self, payload: dict) -> None:
        await self.session.room_io.room.local_participant.publish_data(
            json.dumps(payload),
            topic="commentary.control",
            reliable=True,
        )

    # ==================================================================
    # Persistence — all fire-and-forget; DB latency never stalls speech.
    # ==================================================================
    def _persist(self, role: str, content: str, metadata: dict | None = None) -> None:
        if not self._session_id or not content:
            return
        _fire_and_forget(
            log_conversation_message(self._session_id, role, content, metadata),
            name=f"persist.{role}",
        )
