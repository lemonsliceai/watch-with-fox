"""Time-based triggers that may fire ``CommentaryPipeline.maybe_deliver``.

The scheduler owns the silence-fallback loop, the watchdog, the
post-intro kickoff, and the sentence-trigger gate consulted by the
podcast STT handler. Pipeline knows nothing of Scheduler — Scheduler
calls Pipeline only.
"""

from __future__ import annotations

import asyncio
import logging

from podcast_commentary.agent.commentary import (
    SENTENCE_THRESHOLD,
    CommentaryTimer,
    FullTranscript,
)
from podcast_commentary.agent.commentary_pipeline import CommentaryPipeline
from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.room_state import RoomState
from podcast_commentary.agent.task_supervisor import TaskSupervisor

logger = logging.getLogger("podcast-commentary.scheduler")


SILENCE_FALLBACK_DELAY = CONFIG.timing.silence_fallback_s


# Kickoff delay after intros complete before the scheduler forces the
# first commentary turn. Short enough that the post-intro silence
# doesn't feel like dead air; long enough that STT has had a moment to
# process whatever the video was saying over the intro.
_POST_INTRO_KICKOFF_DELAY_S: float = 3.0


# Watchdog cadence — if no commentary turn has landed within this window
# and the room is listening, force one. Catches edge cases where the
# silence loop silently died, the selector locked up, or a persona got
# stuck in a non-LISTENING phase that later resolved without re-arming
# the loop.
_WATCHDOG_INTERVAL_S: float = 15.0


class CommentaryScheduler:
    """Owns the silence loop, watchdog, kickoff, and sentence-trigger gate."""

    def __init__(
        self,
        *,
        pipeline: CommentaryPipeline,
        room_state: RoomState,
        timer: CommentaryTimer,
        full_transcript: FullTranscript,
        tasks: TaskSupervisor,
    ) -> None:
        self._pipeline = pipeline
        self._room_state = room_state
        self._timer = timer
        self._full_transcript = full_transcript
        self._tasks = tasks

        # Effective silence-fallback delay; mutable via ``set_silence_delay``.
        self._silence_delay: float = SILENCE_FALLBACK_DELAY
        self._silence_task: asyncio.Task | None = None

    def start(self) -> None:
        """Begin the silence loop + watchdog. Caller awaits kickoff separately."""
        self._schedule_silence()
        self._tasks.fire_and_forget(self._watchdog_loop(), name="director_watchdog")

    def set_silence_delay(self, delay: float) -> None:
        """Apply a new silence-fallback delay (used by SettingsController)."""
        self._silence_delay = delay

    # ------------------------------------------------------------------
    # Silence loop — the primary "no transcript landed in a while" trigger
    # ------------------------------------------------------------------
    def rearm_silence(self) -> None:
        """Re-arm the silence loop (called after a turn ends)."""
        if self._room_state.shutting_down:
            return
        self._schedule_silence()

    def _schedule_silence(self) -> None:
        if self._room_state.shutting_down:
            return
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = self._tasks.fire_and_forget(
            self._silence_loop(), name="director_silence"
        )

    async def _silence_loop(self) -> None:
        """Sleep ``_silence_delay`` and try to deliver. Critical invariant:
        this loop must NEVER terminate via early-return while the room is
        alive — otherwise a quiet stretch with no ``speech_end`` re-arm
        leaves the show permanently mute. Bug history: the loop used to
        fire once and exit on any ineligibility check, which combined
        with a non-Latin podcast (sentence trigger never fires) plus a
        min_gap miss to produce 52s of dead air before the watchdog.
        """
        while not self._room_state.shutting_down:
            await asyncio.sleep(self._silence_delay)
            if self._room_state.shutting_down:
                return
            # Skip iterations where the room isn't ready, but keep looping —
            # transient blockers (mid-turn, no transcript yet) are normal.
            if not self._room_state.is_listening() or not self._full_transcript.has_content():
                continue
            await self._pipeline.maybe_deliver(
                trigger_reason="the video has gone quiet — react to what was said",
                energy_level="amused",
            )

    # ------------------------------------------------------------------
    # Post-intro kickoff — break the silence right after intros land
    # ------------------------------------------------------------------
    async def post_intro_kickoff(self) -> None:
        """Fire one guaranteed turn soon after intros so the show actually starts.

        Without this, the window between intros ending and the silence
        loop's first wake stretches and feels like the pair has stalled.
        """
        await asyncio.sleep(_POST_INTRO_KICKOFF_DELAY_S)
        if self._room_state.shutting_down or not self._room_state.is_listening():
            return
        await self._pipeline.maybe_deliver(
            trigger_reason="post-intro kickoff — break the silence and start the show",
            energy_level="amused",
        )

    # ------------------------------------------------------------------
    # Watchdog — last-resort forward-progress guarantor
    # ------------------------------------------------------------------
    async def _watchdog_loop(self) -> None:
        while not self._room_state.shutting_down:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            if self._room_state.shutting_down:
                return
            idle = self._room_state.turn_idle_seconds()
            if idle >= _WATCHDOG_INTERVAL_S and self._room_state.is_listening():
                logger.warning("Watchdog: %.1fs idle — forcing commentary", idle)
                await self._pipeline.maybe_deliver(
                    trigger_reason="watchdog — room was silent too long, step in",
                    energy_level="amused",
                )

    # ------------------------------------------------------------------
    # Sentence trigger — called by the podcast STT handler
    # ------------------------------------------------------------------
    def maybe_trigger_on_sentence(self, sentence_count: int) -> None:
        """Fire commentary if the threshold is met and the gates are open."""
        if (
            sentence_count >= SENTENCE_THRESHOLD
            and self._room_state.is_listening()
            and self._timer.can_comment()
        ):
            self._tasks.fire_and_forget(
                self._pipeline.maybe_deliver(
                    trigger_reason="react to the latest transcript",
                    energy_level="amused",
                ),
                name="sentence_trigger_commentary",
            )


__all__ = ["CommentaryScheduler", "SILENCE_FALLBACK_DELAY"]
