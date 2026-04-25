"""Shared lifecycle state read by every orchestration component.

These flags are consulted by ``IntroSequencer``, ``CommentaryPipeline``,
``CommentaryScheduler``, and the Director's persona-event shims. Bundling
them in one value object keeps the data flow linear instead of forcing
back-references between siblings.
"""

from __future__ import annotations

import asyncio
import time

from podcast_commentary.agent.comedian import FoxPhase, PersonaAgent


class RoomState:
    """Mutable shared state — events + monotonic clock + listening predicate.

    Director is the only component that flips ``shutdown_event`` (via
    ``mark_shutdown``) and ``intros_done`` (via ``mark_intros_done``,
    called by ``IntroSequencer`` once the intro sequence terminates).
    Everyone else only reads these and calls ``mark_turn`` after each
    delivered commentary turn.
    """

    def __init__(self, personas: list[PersonaAgent]) -> None:
        self._personas = personas
        # Set by ``mark_shutdown`` so long awaits (avatar-readiness gates,
        # silence-loop sleeps) can short-circuit instead of blocking the
        # full per-call timeout when the job is already torn down.
        self.shutdown_event: asyncio.Event = asyncio.Event()
        # Set when every intro has been delivered (or skipped). Without
        # this gate, personas default to LISTENING after ``on_enter`` and
        # the silence loop / sentence trigger / watchdog would happily
        # fire commentary in the window before the first intro starts —
        # meaning Fox would deliver a punchline INSTEAD OF its intro.
        self.intros_done: asyncio.Event = asyncio.Event()
        # Monotonic timestamp of the most recent commentary turn (intros
        # do not count). Used by the watchdog to detect dead air.
        self._last_turn_time: float = time.monotonic()

    @property
    def shutting_down(self) -> bool:
        return self.shutdown_event.is_set()

    def mark_shutdown(self) -> None:
        """Signal shutdown; also unblocks intros_done waiters."""
        self.shutdown_event.set()
        # Unblock any ``is_listening`` waiters still gated on intros so
        # pending tasks can observe shutdown and exit cleanly instead of
        # stalling on the avatar-readiness race.
        self.intros_done.set()

    def mark_intros_done(self) -> None:
        self.intros_done.set()

    def mark_turn(self) -> None:
        self._last_turn_time = time.monotonic()

    def turn_idle_seconds(self) -> float:
        return time.monotonic() - self._last_turn_time

    def is_listening(self) -> bool:
        """True iff intros are done AND every persona is in LISTENING.

        The intro gate is load-bearing — see the ``intros_done`` field
        comment for the bug it prevents.
        """
        if not self.intros_done.is_set():
            return False
        return all(p.phase == FoxPhase.LISTENING for p in self._personas)


__all__ = ["RoomState"]
