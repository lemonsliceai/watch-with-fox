"""UserPresenceMonitor — heartbeat watchdog that detects user departure.

The Director has two distinct ways to learn the user has left:

  1. ``participant_disconnected`` — the happy path. The SDK fires this
     when LiveKit observes a clean disconnect, the Director's handler
     trips the shutdown latch.
  2. Heartbeat watchdog — the safety net. If the user's tab is killed
     without a clean disconnect signal (network pull, hard kill,
     OS sleep), the SDK eventually times the participant out — but in
     the meantime the show would keep running into a dead room. This
     monitor polls ``remote_participants`` across every room and
     force-trips the latch once the user has been absent for the
     configured timeout window.

The monitor encapsulates: the last-seen clock, the user-vs-avatar
discrimination (avatars publish under the
``lemonslice-avatar-`` prefix; everything else is a user), the polling
loop, and the timeout decision. The Director stays responsible for
*what* a timeout means (set ``end_reason``, trip the latch).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger("podcast-commentary.director")


# Avatar participants publish under this identity prefix (see persona_runtime
# ``avatar_identity_for``). Anything else disconnecting is the user.
_AVATAR_IDENTITY_PREFIX = "lemonslice-avatar-"


class UserPresenceMonitor:
    """Tracks whether the user is still in any of a job's rooms.

    Construction is cheap; ``run()`` drives the polling loop and only
    returns once ``stop_event`` is set or the timeout fires (in which
    case ``on_timeout`` is invoked before returning).

    ``poll_interval_provider`` is a callable so tests can monkeypatch
    the interval at module scope after construction. ``last_user_seen``
    is a settable attribute so callers can reset the clock at lifecycle
    boundaries (e.g. after the agent finishes connecting).
    """

    def __init__(
        self,
        *,
        rooms_provider: Callable[[], Iterable[Any]],
        timeout_s: float,
        on_timeout: Callable[[], None],
        stop_event: asyncio.Event,
        poll_interval_provider: Callable[[], float],
    ) -> None:
        self._rooms_provider = rooms_provider
        self._timeout_s = timeout_s
        self._on_timeout = on_timeout
        self._stop_event = stop_event
        self._poll_interval_provider = poll_interval_provider
        # Initialised to "now" so the watchdog has a full ``timeout_s`` of
        # grace before its first trip decision, regardless of when the
        # user actually shows up.
        self.last_user_seen: float = time.monotonic()

    def is_user_present(self) -> bool:
        """True iff at least one non-avatar remote participant is live.

        ``remote_participants`` excludes the agent's own
        ``local_participant``, so the only non-avatar identities here are
        real users. Iterating every room (not just the primary) is cheap
        and keeps the heartbeat clock honest if the user ever ends up in
        a non-primary room.
        """
        seen_rooms: set[int] = set()
        for room in self._rooms_provider():
            if id(room) in seen_rooms:
                continue
            seen_rooms.add(id(room))
            participants = getattr(room, "remote_participants", None) or {}
            for participant in participants.values():
                identity = getattr(participant, "identity", "") or ""
                if identity and not identity.startswith(_AVATAR_IDENTITY_PREFIX):
                    return True
        return False

    async def run(self) -> None:
        """Poll until the user is missing past the timeout, or until stopped."""
        while not self._stop_event.is_set():
            if self.is_user_present():
                self.last_user_seen = time.monotonic()
            elif time.monotonic() - self.last_user_seen >= self._timeout_s:
                logger.warning(
                    "User heartbeat missing for %.0fs across all rooms — "
                    "force-tripping shutdown latch",
                    self._timeout_s,
                )
                self._on_timeout()
                return
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_provider(),
                )
            except asyncio.TimeoutError:
                continue
