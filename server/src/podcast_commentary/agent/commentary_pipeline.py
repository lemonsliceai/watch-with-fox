"""Single-flight selector → delivery → control-channel signalling for one turn.

All triggers (silence loop, watchdog, post-intro kickoff, sentence
trigger) funnel through ``CommentaryPipeline.maybe_deliver`` so two
back-to-back triggers can't both win the race and produce a double-tap.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.commentary import CommentaryTimer, FullTranscript
from podcast_commentary.agent.control_channel import ControlChannel
from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.playout_waiter import PlayoutWaiter
from podcast_commentary.agent.room_state import RoomState
from podcast_commentary.agent.selector import SpeakerSelector

logger = logging.getLogger("podcast-commentary.pipeline")


COMMENTARY_PLAYOUT_TIMEOUT = CONFIG.playout.commentary_timeout_s


class CommentaryPipeline:
    """Pick a speaker, deliver one turn, signal start/end on the control channel."""

    def __init__(
        self,
        *,
        personas: list[PersonaAgent],
        room_state: RoomState,
        timer: CommentaryTimer,
        full_transcript: FullTranscript,
        selector: SpeakerSelector,
        control: ControlChannel,
        playout_waiter: PlayoutWaiter,
    ) -> None:
        self._personas = personas
        self._by_name = {p.name: p for p in personas}
        self._room_state = room_state
        self._timer = timer
        self._full_transcript = full_transcript
        self._selector = selector
        self._control = control
        self._playout = playout_waiter

        # Single-flight gate: two transcript chunks landing back-to-back
        # must not both win the selector race and double-tap.
        self._lock = asyncio.Lock()
        self._last_speaker: str | None = None
        self._consecutive_count: int = 0

    async def maybe_deliver(self, *, trigger_reason: str, energy_level: str) -> None:
        """Pick a speaker and deliver one commentary turn — or quietly no-op."""
        if self._room_state.shutting_down:
            return
        async with self._lock:
            if (
                self._room_state.shutting_down
                or not self._room_state.is_listening()
                or not self._timer.can_comment()
            ):
                return

            speaker_name = await self._selector.pick(
                personas=self._personas,
                transcript=self._full_transcript.recent_transcript(),
                trigger_reason=trigger_reason,
                last_speaker=self._last_speaker,
                consecutive_count=self._consecutive_count,
            )
            if self._room_state.shutting_down:
                return

            persona = self._by_name.get(speaker_name)
            if persona is None:
                logger.warning(
                    "Selector returned unknown speaker %r — defaulting to first persona",
                    speaker_name,
                )
                persona = self._personas[0]

            await self._deliver(persona, trigger_reason=trigger_reason, energy_level=energy_level)

    async def _deliver(
        self, persona: PersonaAgent, *, trigger_reason: str, energy_level: str
    ) -> None:
        """Run one commentary turn for ``persona``.

        The ``finally`` block publishes ``commentary_end`` unconditionally
        — the client's Skip button relies on that event to disable, and
        ``agent_state_changed`` does not fire when the playout-waiter's
        ``synthesize_playout_complete`` recovery path runs.
        """
        if self._room_state.shutting_down:
            return
        await self._control.publish_commentary_start(persona.name)
        try:
            co_history, co_label = self._co_speaker_view(persona)

            handle = await persona.deliver_commentary(
                recent_transcript=self._full_transcript.recent_transcript(),
                trigger_reason=trigger_reason,
                energy_level=energy_level,
                co_speaker_history=co_history,
                co_speaker_label=co_label,
            )
            if handle is None:
                return

            # Reset the read cursor AFTER prompt is built (deliver_commentary
            # already read it) so the next persona reacts to NEW podcast text.
            self._full_transcript.reset_sentence_count()
            self._note_speaker(persona.name)

            try:
                await self._playout.wait(
                    persona, handle, timeout=COMMENTARY_PLAYOUT_TIMEOUT, label="commentary"
                )
            finally:
                self._timer.record_speech_end()
                # Reset the watchdog's idle clock so it doesn't double-fire
                # immediately after a long turn.
                self._room_state.mark_turn()
        finally:
            with contextlib.suppress(Exception):
                await self._control.publish_commentary_end(persona.name)

    def _co_speaker_view(self, persona: PersonaAgent) -> tuple[list[str] | None, str | None]:
        """Most-relevant co-persona's recent lines + label, or (None, None) when alone."""
        others = [p for p in self._personas if p.name != persona.name]
        if not others:
            return None, None
        co = others[0]
        return co.commentary_history, co.label

    def _note_speaker(self, name: str) -> None:
        """Bookkeep consecutive-turn streaks for the selector's cap."""
        if name == self._last_speaker:
            self._consecutive_count += 1
        else:
            self._consecutive_count = 1
        self._last_speaker = name


__all__ = ["CommentaryPipeline", "COMMENTARY_PLAYOUT_TIMEOUT"]
