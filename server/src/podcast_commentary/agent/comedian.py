"""PersonaAgent — one comedian instance (Fox or Alien).

A ``PersonaAgent`` owns only what is *intrinsically per-persona*:

  * ``SpeechGate`` — authoritative "is this persona speaking?" gate
  * its own ``FoxPhase`` state machine
  * its own commentary history and rotated comedic angle
  * its own ``llm_node`` override for verbalized sampling

The shared concerns — the podcast STT pipeline, the rolling transcript,
the commentary timer (MIN_GAP / burst), speaker selection, intro
sequencing, and the ``commentary.control`` data channel — all live in
the ``Director`` (see ``director.py``). The Director invokes
``deliver_commentary()`` on whichever PersonaAgent it picks each turn.

This module used to be ~1000 lines of orchestration; the orchestration
moved to the Director and what's left is a thin wrapper around one
LiveKit ``AgentSession``.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import random
import re
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

from livekit.agents import Agent, ModelSettings, llm
from livekit.plugins import groq

from podcast_commentary.agent.fox_config import FoxConfig
from podcast_commentary.agent.prompts import (
    SAMPLING_SENTINEL,
    build_commentary_request,
)
from podcast_commentary.agent.speech_gate import SpeechGate
from podcast_commentary.core.db import log_conversation_message

logger = logging.getLogger("podcast-commentary.persona")


def _fire_and_forget(coro: Any, *, name: str = "") -> asyncio.Task:
    """Schedule a coroutine without awaiting it, but log any exception."""
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


def _read_pushed_duration(node: Any | None) -> float:
    """Read ``_pushed_duration`` from an ``AudioOutput`` node (private attr)."""
    if node is None:
        return 0.0
    value = getattr(node, "_pushed_duration", None)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _deepest_audio_chain(node: Any | None, *, max_depth: int = 8) -> Any | None:
    """Walk ``next_in_chain`` to the deepest ``AudioOutput`` in the chain.

    ``AgentSession.output.audio`` is the *outer* wrapper (e.g.
    ``_SyncedAudioOutput``), but the node actually writing to the wire is
    deeper (e.g. ``DataStreamAudioOutput``). For diagnostic reads like
    ``_pushed_duration`` the inner one is authoritative.
    """
    cur = node
    for _ in range(max_depth):
        nxt = getattr(cur, "next_in_chain", None)
        if nxt is None or nxt is cur:
            return cur
        cur = nxt
    return cur


# ---------------------------------------------------------------------------
# Verbalized-sampling helpers — persona-neutral.
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


# Primary VS format: one candidate per line as ``<p>|<line>``. Only the FIRST
# `|` splits — anything after it is part of the line (commas, quotes, further
# pipes all welcome). Reduces escaping to zero, which is the whole point.
_LINE_CANDIDATE_RE = re.compile(r"^\s*([01](?:\.\d+)?|\.\d+|0|1)\s*\|\s*(.+?)\s*$")

# Last-ditch recovery when the model ignores the format and returns JSON-ish
# text instead. Regex-based (not ``json.loads``) so malformed JSON — the
# actual reason we moved off JSON — still yields usable lines.
_JSON_LINE_RE = re.compile(r'"line"\s*:\s*"((?:[^"\\]|\\.)*)"')
_JSON_P_RE = re.compile(r'"p"\s*:\s*([0-9.]+)')


def _parse_line_delimited(payload: str) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for row in payload.splitlines():
        m = _LINE_CANDIDATE_RE.match(row)
        if m is None:
            continue
        try:
            p = float(m.group(1))
        except ValueError:
            p = 0.0
        line = m.group(2).strip()
        if len(line) >= 2 and line[0] == line[-1] and line[0] in {'"', "'"}:
            line = line[1:-1].strip()
        if line:
            out.append((p, line))
    return out


def _parse_json_fallback(payload: str) -> list[tuple[float, str]]:
    lines = _JSON_LINE_RE.findall(payload)
    probs = _JSON_P_RE.findall(payload)
    out: list[tuple[float, str]] = []
    for i, raw_line in enumerate(lines):
        try:
            p = float(probs[i]) if i < len(probs) else 0.0
        except ValueError:
            p = 0.0
        line = raw_line.replace('\\"', '"').replace("\\\\", "\\").strip()
        if line:
            out.append((p, line))
    return out


def _parse_candidates(raw: str) -> list[tuple[float, str]]:
    """Parse a verbalized-sampling response into ``(probability, line)`` tuples.

    Tries the line-delimited format first, falls back to JSON-ish recovery.
    Returns ``[]`` when nothing parses — caller decides what silence means.
    """
    payload = raw.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
        payload = payload.strip()

    candidates = _parse_line_delimited(payload)
    if not candidates:
        candidates = _parse_json_fallback(payload)
    return candidates


def _select_candidate(raw: str, strategy: str) -> str:
    """Pick one candidate line synchronously (max_prob / top_k_random).

    For ``"judge"`` selection see ``PersonaAgent._judge_select`` — that path
    needs an LLM round-trip and lives on the agent so it can reuse the
    persona's history and the chat context for transcript anchoring.

    Never returns the raw model output: on total parse failure we return
    ``""`` so the caller pipes silence to TTS instead of making the avatar
    recite the format envelope out loud (the 80-second JSON-soliloquy bug).
    """
    candidates = _parse_candidates(raw)
    if not candidates:
        logger.warning(
            "VS parse recovered 0 candidates — dropping turn (preview=%r)",
            raw[:200],
        )
        return ""

    if strategy == "top_k_random":
        top = sorted(candidates, key=lambda c: c[0], reverse=True)[:3]
        p, line = random.choice(top)
    else:
        p, line = max(candidates, key=lambda c: c[0])

    logger.info(
        "VS picked candidate (strategy=%s, p=%.2f, of %d): %s",
        strategy,
        p,
        len(candidates),
        line[:120],
    )
    return line


# ---------------------------------------------------------------------------
# Judge selection — second LLM round-trip that reranks the parsed candidates
# against a 3-axis rubric (anchor / fresh / snap). Lives at module scope so
# the prompt is grep-able alongside the persona prompts; the LLM client and
# context-extraction are on PersonaAgent because they need per-persona state.
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM = (
    "You are the comedy judge for Couchverse — a show where AI comedians "
    "riff on whatever audio the user is playing. Your only job is to pick "
    "the FUNNIEST candidate line.\n\n"
    "Score each candidate 1-5 on three axes; pick the highest TOTAL:\n"
    "- ANCHOR: does it latch onto a SPECIFIC word, name, number, or claim "
    "from the transcript? Generic line that could land on any clip = 1. "
    "Sharp specific reference = 5.\n"
    "- FRESH: does it use a different opener, shape, and joke structure "
    "than the persona's recent lines? Repeat shape = 1. New beat = 5.\n"
    "- SNAP: does the surprise land on the LAST word? Predictable last "
    "beat = 1. Last-word twist = 5.\n\n"
    'Reply with strict JSON only: {"winner": <1-N>, "reason": "<short>"}. '
    "No prose, no markdown, no extra keys."
)


# Match the [LATEST TRANSCRIPT — ...] block emitted by ``prompts.py`` so the
# judge can score the ANCHOR axis against the same text the model saw.
_TRANSCRIPT_BLOCK_RE = re.compile(
    r"\[LATEST TRANSCRIPT[^\]]*\]\n(.*?)(?=\n\n\[|\Z)",
    re.DOTALL,
)


def _extract_transcript_block(chat_ctx: llm.ChatContext) -> str:
    """Pull the LATEST TRANSCRIPT block out of the most recent user message."""
    for item in reversed(chat_ctx.items):
        if not isinstance(item, llm.ChatMessage) or item.role != "user":
            continue
        text = item.text_content or ""
        m = _TRANSCRIPT_BLOCK_RE.search(text)
        if m:
            return m.group(1).strip()
    return ""


def _parse_judge_winner(raw: str, n_candidates: int) -> int:
    """Parse the judge's JSON reply into a 0-based index, or ``-1`` on failure."""
    payload = raw.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
        payload = payload.strip()
    try:
        data = json.loads(payload)
        winner = int(data.get("winner", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning("Judge LLM produced invalid JSON: %r", raw[:120])
        return -1
    idx = winner - 1
    if 0 <= idx < n_candidates:
        return idx
    logger.warning("Judge LLM picked out-of-range winner=%d (of %d)", winner, n_candidates)
    return -1


class FoxPhase(enum.Enum):
    """Per-persona lifecycle phases.

    Each PersonaAgent runs its own state machine. The Director coordinates
    *across* personas but never reaches inside a persona's phase.
    """

    INTRO = "intro"
    LISTENING = "listening"
    COMMENTATING = "commentating"


_VALID_TRANSITIONS: dict[FoxPhase, set[FoxPhase]] = {
    FoxPhase.INTRO: {FoxPhase.LISTENING},
    FoxPhase.LISTENING: {FoxPhase.COMMENTATING, FoxPhase.INTRO},
    FoxPhase.COMMENTATING: {FoxPhase.LISTENING},
}


class PersonaAgent(Agent):
    """One comedian — Fox or Alien — bound to one ``AgentSession``."""

    def __init__(
        self,
        *,
        config: FoxConfig,
        session_id: str | None = None,
        on_speech_start: Callable[["PersonaAgent"], None] | None = None,
        on_speech_end: Callable[["PersonaAgent"], None] | None = None,
        on_turn_finalised: Callable[["PersonaAgent", str, str | None], Awaitable[None]]
        | None = None,
    ) -> None:
        super().__init__(instructions=config.persona.system_prompt)
        self._config = config
        self._session_id = session_id

        # Director-supplied callbacks. None when running standalone (tests).
        self._on_speech_start_cb = on_speech_start
        self._on_speech_end_cb = on_speech_end
        self._on_turn_finalised_cb = on_turn_finalised

        # Per-persona state.
        self._commentary_history: list[str] = []
        self._recent_angles: list[str] = []
        self._pending_angle_name: str | None = None
        self._gate: SpeechGate | None = None
        self._phase = FoxPhase.LISTENING
        # UI-driven reply-length preference — "short" | "long" | None (normal).
        # Director sets this from the extension's settings message.
        self._length_hint: str | None = None
        # Lazy-built when ``sampling.selection == "judge"`` — second Groq
        # client used for the rerank round-trip. None until first use.
        self._judge_llm: groq.LLM | None = None

        # Set by ``on_enter`` so the Director can wait for both personas to
        # finish initial composition before delivering the coordinated intro.
        self.ready: asyncio.Event = asyncio.Event()

    # ==================================================================
    # Public read-only state
    # ==================================================================
    @property
    def config(self) -> FoxConfig:
        return self._config

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def label(self) -> str:
        return self._config.persona.speaker_label or self._config.name

    @property
    def phase(self) -> FoxPhase:
        return self._phase

    @property
    def gate(self) -> SpeechGate:
        if self._gate is None:
            raise RuntimeError("PersonaAgent.gate accessed before on_enter")
        return self._gate

    @property
    def is_speaking(self) -> bool:
        return self._gate is not None and self._gate.is_speaking

    @property
    def commentary_history(self) -> list[str]:
        """Read-only view (the Director shows it as co-speaker context)."""
        return list(self._commentary_history)

    # ==================================================================
    # LLM node override — verbalized sampling
    # ==================================================================
    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str]:
        default_node = Agent.default.llm_node(self, chat_ctx, tools, model_settings)

        if self._config.sampling.num_candidates <= 1 or not _prompt_uses_sampling(chat_ctx):
            async for chunk in default_node:
                yield chunk
            return

        buf: list[str] = []
        async for chunk in default_node:
            text = _chunk_text(chunk)
            if text:
                buf.append(text)
        raw = "".join(buf)

        if self._config.sampling.selection == "judge":
            winner = await self._judge_select(raw, chat_ctx)
        else:
            winner = _select_candidate(raw, self._config.sampling.selection)
        yield winner

    # ------------------------------------------------------------------
    # Judge selection — async because it needs an LLM round-trip.
    # ------------------------------------------------------------------
    async def _judge_select(self, raw: str, chat_ctx: llm.ChatContext) -> str:
        """Rerank candidates with the judge LLM; fall back to max_prob on failure."""
        candidates = _parse_candidates(raw)
        if not candidates:
            logger.warning(
                "VS parse recovered 0 candidates — dropping turn (preview=%r)",
                raw[:200],
            )
            return ""
        if len(candidates) == 1:
            return candidates[0][1]

        transcript = _extract_transcript_block(chat_ctx)
        idx = -1
        try:
            idx = await asyncio.wait_for(
                self._judge_pick(candidates, transcript=transcript),
                timeout=self._config.sampling.judge_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning("Judge LLM timed out — falling back to max_prob")
        except Exception:
            logger.warning("Judge LLM raised — falling back to max_prob", exc_info=True)

        if 0 <= idx < len(candidates):
            p, line = candidates[idx]
            logger.info(
                "Judge picked candidate %d (p=%.2f, of %d): %s",
                idx + 1,
                p,
                len(candidates),
                line[:120],
            )
            return line

        p, line = max(candidates, key=lambda c: c[0])
        logger.info("Judge fallback (max_prob, p=%.2f, of %d): %s", p, len(candidates), line[:120])
        return line

    async def _judge_pick(self, candidates: list[tuple[float, str]], *, transcript: str) -> int:
        """Ask the judge LLM which candidate to ship. Returns 0-based index."""
        cand_block = "\n".join(f"[{i + 1}] {line}" for i, (_, line) in enumerate(candidates))
        recent = (
            "\n".join(f"- {c}" for c in self._commentary_history[-5:])
            if self._commentary_history
            else "(none yet)"
        )
        user_prompt = (
            f"PERSONA: {self.label}\n\n"
            f"LATEST TRANSCRIPT:\n{transcript or '(silent)'}\n\n"
            f"PERSONA'S RECENT LINES (avoid these shapes):\n{recent}\n\n"
            f"CANDIDATES:\n{cand_block}\n\n"
            'Reply strict JSON: {"winner": <1-N>, "reason": "<short>"}.'
        )
        chat_ctx = llm.ChatContext.empty()
        chat_ctx.add_message(role="system", content=_JUDGE_SYSTEM)
        chat_ctx.add_message(role="user", content=user_prompt)

        judge = self._ensure_judge_llm()
        buf: list[str] = []
        async with judge.chat(chat_ctx=chat_ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    buf.append(chunk.delta.content)
        return _parse_judge_winner("".join(buf), len(candidates))

    def _ensure_judge_llm(self) -> groq.LLM:
        if self._judge_llm is None:
            self._judge_llm = groq.LLM(
                model=self._config.sampling.judge_model,
                max_completion_tokens=80,
            )
        return self._judge_llm

    # ==================================================================
    # Lifecycle
    # ==================================================================
    async def on_enter(self) -> None:
        """Compose the SpeechGate and signal readiness — but do NOT speak.

        The Director owns intros so we can sequence them across personas
        instead of having Fox and Alien talk over each other in second 1.
        """
        self._gate = SpeechGate(
            self.session,
            on_released=self._on_speech_released,
            name=self._config.name,
        )
        self.session.on("conversation_item_added", self._on_conversation_item_added)
        self.session.on("agent_state_changed", self._on_agent_state_changed)
        self.ready.set()

    # ==================================================================
    # Director-driven actions
    # ==================================================================
    def speak_intro(self) -> Any:
        """Deliver this persona's intro line. Director calls at most once.

        Speaks a static, pre-authored line via ``session.say`` rather than
        going through the LLM. Intros are the most load-bearing beat of the
        show (first impression, sequenced across personas) and must be
        reliable — static audio is short and predictable, which keeps it
        well inside the playout-timeout window that triggers our LemonSlice
        multi-avatar RPC fallback. Returns the SpeechHandle so the Director
        can ``wait_for_playout`` with its own timeout safety net.
        """
        self._set_phase(FoxPhase.INTRO)
        return self.gate.say(text=self._config.persona.intro_line)

    async def deliver_commentary(
        self,
        *,
        recent_transcript: str,
        trigger_reason: str,
        energy_level: str,
        co_speaker_history: list[str] | None = None,
        co_speaker_label: str | None = None,
    ) -> Any:
        """Generate + speak one commentary line. Returns the SpeechHandle.

        The Director picks angle rotation via this persona's own bank and
        bookkeeping. We log the prompt + return the handle so the Director
        can await playout with its global timer.
        """
        from podcast_commentary.agent.angles import pick_angle

        angle = pick_angle(self._recent_angles, config=self._config)
        self._pending_angle_name = angle
        prompt = build_commentary_request(
            config=self._config,
            recent_transcript=recent_transcript,
            commentary_history=self._commentary_history,
            trigger_reason=trigger_reason,
            energy_level=energy_level,
            angle=angle,
            co_speaker_history=co_speaker_history,
            co_speaker_label=co_speaker_label,
            length_hint=self._length_hint,
        )
        self._set_phase(FoxPhase.COMMENTATING)
        return self.gate.speak(prompt=prompt)

    def interrupt(self) -> None:
        """Cut off the current turn if any. Safe to call from any thread."""
        if self._gate is not None:
            self._gate.interrupt()

    def force_listening(self) -> None:
        """Recovery hook: force phase to LISTENING and interrupt any live handle.

        Used when an avatar hangs on ``playback_finished`` and we can't wait
        for the speech handle to resolve cleanly. Advancing the phase
        immediately unblocks ``Director._room_is_listening`` so the silence
        loop and sentence-triggered commentary can fire again. The
        ``SpeechGate`` identity check in ``_on_done`` means the late
        resolution of the stuck handle becomes a harmless no-op once we've
        started a new turn.

        Prefer ``synthesize_playout_complete`` over this — it lets
        already-pushed audio finish reaching the avatar instead of cutting
        it off mid-sentence. ``force_listening`` is the last-resort escape
        hatch when a handle is truly stuck and audio isn't flowing.
        """
        if self._phase in (FoxPhase.INTRO, FoxPhase.COMMENTATING):
            self._set_phase(FoxPhase.LISTENING)
        if self._gate is not None:
            self._gate.interrupt()

    def synthesize_playout_complete(self) -> tuple[float, float]:
        """Manually fire ``playback_finished`` on this persona's audio output.

        LiveKit's ``DataStreamAudioOutput`` (the sink installed by
        ``lemonslice.AvatarSession``) normally marks a turn done when the
        avatar sends the ``lk.playback_finished`` RPC back. LemonSlice's
        *second* avatar in a multi-avatar room is unreliable about sending
        that RPC — see GitHub livekit/agents #3510 and #4315. When the RPC
        is missing, ``SpeechHandle.wait_for_playout`` blocks forever.

        The audio chain is:
        ``AgentSession → _SyncedAudioOutput → DataStreamAudioOutput``.
        Both layers track ``_pushed_duration`` (private) — the outer for
        transcript sync, the inner for wire bytes. Calling
        ``on_playback_finished`` on the outer wrapper automatically
        propagates via the framework's ``next_in_chain`` event plumbing,
        but we walk to the deepest (DataStream) layer to read the most
        authoritative duration — that's the one that tells us whether
        frames actually reached the wire.

        Returns ``(outer_pushed, inner_pushed)`` so callers can log both
        and tell "audio never flowed" (both 0) apart from "audio flowed
        but vendor never confirmed" (both > 0).
        """
        audio = self._audio_output()
        if audio is None:
            return 0.0, 0.0
        outer = _read_pushed_duration(audio)
        inner = _read_pushed_duration(_deepest_audio_chain(audio))
        # Use whichever is larger as the reported position — both should
        # match in normal operation, but the deeper layer is closest to
        # the wire and therefore the ground truth.
        position = max(outer, inner)
        try:
            audio.on_playback_finished(playback_position=position, interrupted=False)
        except Exception:
            logger.debug("synthesize_playout_complete failed", exc_info=True)
            return 0.0, 0.0
        return outer, inner

    def _audio_output(self) -> Any | None:
        """Return this persona's AgentSession audio output (or None if gone)."""
        session = getattr(self, "session", None)
        output = getattr(session, "output", None) if session is not None else None
        return getattr(output, "audio", None) if output is not None else None

    def set_length_hint(self, level: str | None) -> None:
        """Store the UI's reply-length preference for the next turn."""
        self._length_hint = level

    # ==================================================================
    # Phase transitions
    # ==================================================================
    def _set_phase(self, new: FoxPhase) -> None:
        old = self._phase
        if old is new:
            return
        valid = _VALID_TRANSITIONS.get(old, set())
        if new not in valid:
            logger.error(
                "%s illegal phase transition: %s → %s (allowed: %s)",
                self._config.name,
                old.value,
                new.value,
                {v.value for v in valid},
            )
            return
        self._phase = new
        logger.info("%s phase: %s → %s", self._config.name, old.value, new.value)

    def _on_speech_released(self) -> None:
        """SpeechGate fires this when the current handle resolves."""
        if self._phase in (FoxPhase.INTRO, FoxPhase.COMMENTATING):
            self._set_phase(FoxPhase.LISTENING)

    # ==================================================================
    # AgentSession events → Director callbacks
    # ==================================================================
    def _on_agent_state_changed(self, ev: Any) -> None:
        """Forward real audio start/end events to the Director's shared timer."""
        logger.info(
            "%s agent state: %s -> %s (phase=%s, is_speaking=%s)",
            self._config.name,
            ev.old_state,
            ev.new_state,
            self._phase.value,
            self.is_speaking,
        )
        started = ev.new_state == "speaking" and ev.old_state != "speaking"
        finished = ev.old_state == "speaking" and ev.new_state == "listening"
        if started and self._on_speech_start_cb is not None:
            try:
                self._on_speech_start_cb(self)
            except Exception:
                logger.debug("on_speech_start callback raised", exc_info=True)
        if finished:
            # Phase reset BEFORE the Director callback. The Director's
            # `_on_persona_speech_end` checks `_room_is_listening()` to
            # decide whether to re-arm the silence loop — and that check
            # demands every persona be in LISTENING. Calling the callback
            # first leaves us in COMMENTATING/INTRO, the gate fails, and
            # the silence loop never re-arms (compounding the bug where
            # the loop dies on early-return).
            if self._phase in (FoxPhase.INTRO, FoxPhase.COMMENTATING):
                self._on_speech_released()
            if self._on_speech_end_cb is not None:
                try:
                    self._on_speech_end_cb(self)
                except Exception:
                    logger.debug("on_speech_end callback raised", exc_info=True)

    def _on_conversation_item_added(self, ev: Any) -> None:
        """Capture finalised assistant messages — local history + Director hook."""
        agent_text = self._extract_assistant_text(ev)
        if agent_text is None:
            return

        self._record_commentary(agent_text)
        self._rotate_angle()
        self._flush_chat_context()

        # Notify the Director so it can update labeled history shown to the
        # other persona's prompt and the speaker-selection judge.
        if self._on_turn_finalised_cb is not None:
            angle = self._pending_angle_name
            _fire_and_forget(
                self._on_turn_finalised_cb(self, agent_text, angle),
                name=f"persona.turn_finalised.{self._config.name}",
            )

    @staticmethod
    def _extract_assistant_text(ev: Any) -> str | None:
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "type", None) != "message":
            return None
        if getattr(item, "role", None) != "assistant":
            return None
        text = (getattr(item, "text_content", None) or "").strip()
        return text or None

    def _record_commentary(self, agent_text: str) -> None:
        logger.info(
            "=== %s SAID ===\n%s\n=== END %s SAID ===",
            self._config.name.upper(),
            agent_text,
            self._config.name.upper(),
        )
        self._commentary_history.append(agent_text)
        self._commentary_history = self._commentary_history[
            -self._config.context.comment_memory_size :
        ]

        meta: dict[str, Any] = {"persona": self._config.name}
        if self._pending_angle_name:
            meta["angle"] = self._pending_angle_name
        self._persist("agent", agent_text, meta)

    def _rotate_angle(self) -> None:
        if self._pending_angle_name:
            self._recent_angles.append(self._pending_angle_name)
            self._recent_angles = self._recent_angles[-self._config.persona.angle_lookback :]
        # NOTE: do NOT clear _pending_angle_name yet — the Director's
        # turn_finalised callback reads it for logging. It's overwritten
        # on the next deliver_* call.

    def _flush_chat_context(self) -> None:
        """Reset persistent chat context after each turn (see ComedianAgent docstring)."""
        self._chat_ctx = llm.ChatContext.empty()

    # ==================================================================
    # Persistence — fire-and-forget
    # ==================================================================
    def _persist(self, role: str, content: str, metadata: dict | None = None) -> None:
        if not self._session_id or not content:
            return
        _fire_and_forget(
            log_conversation_message(self._session_id, role, content, metadata),
            name=f"persist.{role}.{self._config.name}",
        )
