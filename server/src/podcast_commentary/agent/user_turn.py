"""Push-to-talk turn tracker.

Encapsulates the full hold-to-talk state machine:

  * `start()`   — user pressed the button; open the talk window and
                  (optionally) interrupt any in-flight Fox turn.
  * `end()`     — user released the button; schedule a grace timer that
                  commits the user turn after 1.5 s (enough for trailing
                  STT finals to land) and invokes `on_committed(text)`.
  * `buffer(t)` — fallback capture for `on_user_turn_completed` while the
                  talk window is open; used if `commit_user_turn` returns
                  empty.

The token dance (`_token`) supersedes any in-flight grace task when a new
`start()` or `end()` arrives, fixing the race where a stale grace from
talk-end #1 could flip `talking=False` mid-second-utterance.

Factored out of `ComedianAgent` so the fragile bookkeeping has one home.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from livekit.agents.voice import AgentSession

logger = logging.getLogger("podcast-commentary.user_turn")

# Grace window after `user_talk_end` before we commit the user turn. Long
# enough for trailing STT finals to land; short enough that Fox's reply
# feels responsive.
GRACE_SECONDS = 1.5


class UserTurnTracker:
    """State machine for hold-to-talk user speech."""

    def __init__(
        self,
        *,
        session: AgentSession,
        on_committed: Callable[[str], Awaitable[None]],
        on_start: Callable[[], None] | None = None,
        on_empty: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._on_committed = on_committed
        self._on_start = on_start
        self._on_empty = on_empty
        self._talking = False
        self._buffer: list[str] = []
        # Every start/end bumps this counter; grace tasks capture it at spawn
        # and only commit if they're still current. Prevents a stale grace
        # from a prior talk_end clobbering a live talk_start.
        self._token: int = 0

    @property
    def talking(self) -> bool:
        """True while the user is holding the push-to-talk button (or
        within the grace window after release, until commit)."""
        return self._talking

    def start(self) -> None:
        """Open the talk window.

        Optional `on_start` callback (used to interrupt Fox mid-turn)
        fires *before* any state is mutated, so listeners see a consistent
        view of the previous state.
        """
        logger.info("User started talking")
        if self._on_start is not None:
            try:
                self._on_start()
            except Exception:
                logger.debug("on_start callback raised", exc_info=True)
        self._token += 1
        self._talking = True
        self._buffer.clear()

    def end(self) -> None:
        """User released the button — start the grace-and-commit timer."""
        logger.info("User stopped talking")
        self._token += 1
        token = self._token
        asyncio.create_task(self._grace_and_commit(token))

    def buffer(self, text: str) -> None:
        """Capture a trailing STT final while the talk window is open.

        Stray events that arrive outside the window (before start or after
        commit) are dropped to keep the commentary transcript clean.
        """
        if not text:
            return
        if self._talking:
            self._buffer.append(text)
        else:
            logger.info(
                "Dropping transcript (user not in talk window): %r", text[:80]
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _grace_and_commit(self, token: int) -> None:
        """Wait out the grace window, then commit the user turn.

        Uses `session.commit_user_turn(skip_reply=True)` — the framework's
        authoritative "the user is done, flush STT" API. `skip_reply=True`
        prevents the framework from generating its own reply so we can
        build our own prompt (with podcast context + angle).
        """
        await asyncio.sleep(GRACE_SECONDS)
        if token != self._token:
            logger.info(
                "Grace task superseded (token=%d now=%d) — skipping close",
                token, self._token,
            )
            return
        self._talking = False

        try:
            committed = await self._session.commit_user_turn(skip_reply=True)
        except Exception:
            logger.warning(
                "commit_user_turn failed — falling back to buffered finals",
                exc_info=True,
            )
            committed = ""

        # Re-check after the await — a new start() may have fired while
        # commit_user_turn was in flight, making this grace task stale.
        if token != self._token:
            logger.info(
                "Grace task superseded during commit (token=%d now=%d) — "
                "dropping result",
                token, self._token,
            )
            return

        logger.info(
            "commit_user_turn returned: %r (buffered finals: %r)",
            committed, self._buffer,
        )

        user_text = (committed or "").strip() or " ".join(self._buffer).strip()
        self._buffer.clear()

        if not user_text:
            logger.info(
                "User turn committed with empty transcript — no mic audio was "
                "heard. Check: did a 'Track subscribed [source=SOURCE_MICROPHONE]' "
                "log appear during the talk window? Did any 'STT transcription' "
                "events fire? If neither, the session STT never got audio."
            )
            if self._on_empty is not None:
                try:
                    self._on_empty()
                except Exception:
                    logger.debug("on_empty callback raised", exc_info=True)
            return

        logger.info("User said: %s", user_text)
        await self._on_committed(user_text)
