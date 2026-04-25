"""Apply UI settings (frequency / length) onto the live timer + personas.

The frequency preset scales both the per-turn cool-down (``MIN_GAP``)
and the silence-fallback delay so Quiet/Chatty noticeably change *when*
a persona steps in. Length is stashed on each PersonaAgent and read by
the prompt builder on the next turn — no restart needed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.commentary import MIN_GAP, CommentaryTimer

logger = logging.getLogger("podcast-commentary.settings")


# Chattiness presets from the UI. Each entry scales (gap_multiplier,
# silence_multiplier). With MIN_GAP=10s, this lands the gap at ~5s /
# ~10s / ~15s for Chatty / Normal / Quiet — "normal" leaves the
# config-derived defaults untouched.
_FREQUENCY_PRESETS: dict[str, tuple[float, float]] = {
    "quiet": (1.5, 1.5),
    "normal": (1.0, 1.0),
    "chatty": (0.5, 0.5),
}


class SettingsController:
    """Mutates the timer's ``min_gap`` and the scheduler's silence delay in place.

    The silence-delay knob is injected as a callback so this controller
    doesn't import the scheduler module — keeps the dependency direction
    one-way.
    """

    def __init__(
        self,
        *,
        timer: CommentaryTimer,
        personas: list[PersonaAgent],
        base_silence_delay: float,
        apply_silence_delay: Callable[[float], None],
    ) -> None:
        self._timer = timer
        self._personas = personas
        self._base_silence_delay = base_silence_delay
        self._apply_silence_delay = apply_silence_delay

    def update(self, *, frequency: str | None = None, length: str | None = None) -> None:
        if frequency in _FREQUENCY_PRESETS:
            gap_mult, silence_mult = _FREQUENCY_PRESETS[frequency]
            self._timer.min_gap = MIN_GAP * gap_mult
            new_silence = self._base_silence_delay * silence_mult
            self._apply_silence_delay(new_silence)
            logger.info(
                "Frequency → %s (min_gap=%.1fs, silence_fallback=%.1fs)",
                frequency,
                self._timer.min_gap,
                new_silence,
            )
        elif frequency is not None:
            logger.warning("Ignoring unknown frequency setting: %r", frequency)

        if length in ("short", "normal", "long"):
            hint = length if length != "normal" else None
            for p in self._personas:
                p.set_length_hint(hint)
            logger.info("Length → %s", length)
        elif length is not None:
            logger.warning("Ignoring unknown length setting: %r", length)


__all__ = ["SettingsController"]
