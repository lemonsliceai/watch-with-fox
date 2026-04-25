"""Phase-aware skip handling.

The Chrome extension's "Skip commentary" button fires a ``skip`` control
message. Naive handling — interrupt every persona — breaks the intro
ritual, because a click landing between Fox's intro ending and Alien's
intro starting would cut Alien off mid-sentence.

``SkipCoordinator`` scopes interrupts to the set of phases the user
*meant* to skip: only active commentary turns. Intros are protected by
construction.

Keeping this in its own module decouples the Director's orchestration
logic from the skip-policy decision, and makes the policy trivially
testable with fake personas.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from podcast_commentary.agent.comedian import FoxPhase, PersonaAgent

logger = logging.getLogger("podcast-commentary.skip")


# Phases the Skip button is allowed to cut off. Intros and idle
# listening are explicitly NOT in this set.
_SKIPPABLE_PHASES: frozenset[FoxPhase] = frozenset({FoxPhase.COMMENTATING})


class SkipCoordinator:
    """Interrupt only personas whose current phase is user-skippable."""

    def __init__(self, personas: Iterable[PersonaAgent]) -> None:
        self._personas = list(personas)

    def request_skip(self) -> None:
        """Interrupt every persona in a skippable phase. No-op otherwise."""
        cut: list[str] = []
        for p in self._personas:
            if p.phase in _SKIPPABLE_PHASES:
                p.interrupt()
                cut.append(p.name)
        if cut:
            logger.info("Skip request honored for: %s", ", ".join(cut))
        else:
            logger.info("Skip request ignored — no persona in a skippable phase")


__all__ = ["SkipCoordinator"]
