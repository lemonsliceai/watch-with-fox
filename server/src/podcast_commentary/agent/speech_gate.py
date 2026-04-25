"""Authoritative "is Fox speaking?" gate.

Owns the current `SpeechHandle` and exposes a `SpeechHandle.done()`-backed
`is_speaking` property. Every producer that wants to speak (the silence loop,
the podcast-transcript consumer, the user-reply path, the intro) goes through
`speak()`. Every terminal case — successful playout, empty-LLM short-circuit,
interruption by the user, or an internal error — resolves the handle's
`done()` future, so the gate can never get stuck True.

Factored out of `ComedianAgent` so the gate logic has one home and one
invariant: `is_speaking` iff `_current_speech is not None and not done()`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from livekit.agents import llm
from livekit.agents.voice import AgentSession, SpeechHandle

logger = logging.getLogger("podcast-commentary.speech_gate")


class SpeechGate:
    """Single source of truth for one persona's speaking state.

    With multiple personas in the room, each owns its own gate keyed by
    ``name`` so log lines stay disambiguated (``[fox]`` vs ``[alien]``).
    """

    def __init__(
        self,
        session: AgentSession,
        on_released: Callable[[], None] | None = None,
        name: str = "",
    ) -> None:
        self._session = session
        self._current: SpeechHandle | None = None
        self._on_released = on_released
        self._name = name or "speaker"

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------
    @property
    def is_speaking(self) -> bool:
        """True iff a Fox turn is queued or playing.

        Backed by `SpeechHandle.done()`, which the framework resolves on
        every terminal case. No flags to get stuck, no race between "set
        True before await" and an event that never fires.
        """
        return self._current is not None and not self._current.done()

    @property
    def current(self) -> SpeechHandle | None:
        """The live handle, for callers that need to `wait_for_playout` or
        `interrupt` it. Callers should not poke at it otherwise."""
        return self._current

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def speak(
        self,
        *,
        prompt: str,
        allow_interruptions: bool = False,
    ) -> SpeechHandle | None:
        """Kick off an agent turn.

        Passes an empty chat_ctx so the LLM only sees `[SYSTEM, USER]` per
        turn — the framework's `update_instructions` inserts the agent's
        SYSTEM message, and `user_input=prompt` is wrapped as a USER
        ChatMessage. No accumulated ASSISTANT history from prior turns (that
        was the bug where chat-completion models reliably returned empty).

        Synchronously:
          1. Assigns the handle to `self._current` so `is_speaking` flips
             True *before* any `await` the caller makes. Closes the race
             window where a podcast transcript could land between "I
             decided to speak" and "the framework said I'm speaking".
          2. Registers `_on_done` so every terminal case flips the gate
             back to False.

        `allow_interruptions` defaults to False so podcast audio can't step
        on Fox mid-sentence. Set True for user-reply turns — the user
        should be able to cut him off with a fresh hold-to-talk.

        Returns ``None`` if the underlying ``AgentSession`` has already
        been closed (user disconnected between "decide to speak" and
        "start speaking"). Callers propagate the ``None`` up and skip
        ``wait_for_playout`` — there's nothing to wait for.
        """
        logger.info(
            "=== %s SPEAK PROMPT ===\n%s\n=== END SPEAK PROMPT ===",
            self._name.upper(),
            prompt,
        )
        try:
            handle = self._session.generate_reply(
                user_input=prompt,
                chat_ctx=llm.ChatContext.empty(),
                allow_interruptions=allow_interruptions,
            )
        except RuntimeError as exc:
            # The framework raises a bare RuntimeError with this exact
            # message when the session was closed (e.g. by participant
            # disconnect) before we got here. Treat it as "nothing to say"
            # rather than crashing the Director's silence/selection loop.
            if "AgentSession isn't running" in str(exc):
                logger.debug("%s speak skipped — session closed", self._name)
                return None
            raise
        self._current = handle
        handle.add_done_callback(self._on_done)
        return handle

    def say(
        self,
        *,
        text: str,
        allow_interruptions: bool = False,
    ) -> SpeechHandle | None:
        """Speak a static line verbatim (no LLM) and track the handle.

        Mirrors ``speak()`` but goes through ``session.say()`` instead of
        ``generate_reply``. Used for intros where predictability beats
        variety — static audio is short (~3-5s), avoids the LLM+TTS+avatar
        latency spike, and therefore sails under the playout timeout that
        kicks the LemonSlice multi-avatar ``lk.playback_finished`` fallback.
        """
        logger.info(
            "=== %s SAY (static) ===\n%s\n=== END SAY ===",
            self._name.upper(),
            text,
        )
        try:
            handle = self._session.say(
                text,
                allow_interruptions=allow_interruptions,
            )
        except RuntimeError as exc:
            if "AgentSession isn't running" in str(exc):
                logger.debug("%s say skipped — session closed", self._name)
                return None
            raise
        self._current = handle
        handle.add_done_callback(self._on_done)
        return handle

    def interrupt(self) -> None:
        """Cut off the current turn if there is one.

        ``force=True`` bypasses ``allow_interruptions`` so commentary
        handles (which disable auto-interruption to prevent podcast audio
        bleed via VAD) can still be cut off by an explicit user action
        (the Skip button). Safe to call when nothing is speaking (no-ops).
        """
        handle = self._current
        if handle is None or handle.done():
            return
        try:
            handle.interrupt(force=True)
        except Exception:
            logger.debug("Failed to interrupt current speech", exc_info=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_done(self, handle: SpeechHandle) -> None:
        """Clear `_current` when its handle resolves.

        Identity-check so a stale done-callback from an interrupted turn
        can't wipe a newly-queued one. Fires `on_released` so the owning
        agent can transition its phase back to LISTENING.
        """
        if self._current is handle:
            self._current = None
            logger.info(
                "%s speech handle done (interrupted=%s) — gate released",
                self._name,
                getattr(handle, "interrupted", False),
            )
            if self._on_released is not None:
                try:
                    self._on_released()
                except Exception:
                    logger.debug("on_released callback raised", exc_info=True)
