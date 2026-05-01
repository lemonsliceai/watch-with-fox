"""Verbalized-sampling parsing and selection — persona-neutral.

The PersonaAgent's ``llm_node`` override asks the LLM for N candidate
lines in a known format (``<probability>|<line>`` per row, JSON-ish
fallback), then picks one. The parsing rules and the synchronous
selection strategies (``max_prob`` / ``top_k_random``) live here so
PersonaAgent stays focused on per-persona state.

The async ``judge`` selection strategy stays on PersonaAgent because it
needs an LLM round-trip seeded with persona history and chat context.
This module exports the building blocks that path uses:
``parse_candidates`` (parsing), ``extract_transcript_block`` (anchor
extraction from ChatContext), ``parse_judge_winner`` (judge response
decoding) and ``JUDGE_SYSTEM`` (the judge's system prompt).
"""

from __future__ import annotations

import json
import logging
import random
import re
from typing import Any

from livekit.agents import llm

from podcast_commentary.agent.prompts import SAMPLING_SENTINEL

logger = logging.getLogger("podcast-commentary.persona")


def prompt_uses_sampling(chat_ctx: llm.ChatContext) -> bool:
    """True when the most recent user message carries the sampling sentinel."""
    for item in reversed(chat_ctx.items):
        if not isinstance(item, llm.ChatMessage) or item.role != "user":
            continue
        text = item.text_content or ""
        return SAMPLING_SENTINEL in text
    return False


def chunk_text(chunk: Any) -> str:
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


def _strip_code_fence(payload: str) -> str:
    payload = payload.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
        payload = payload.strip()
    return payload


def parse_candidates(raw: str) -> list[tuple[float, str]]:
    """Parse a verbalized-sampling response into ``(probability, line)`` tuples.

    Tries the line-delimited format first, falls back to JSON-ish recovery.
    Returns ``[]`` when nothing parses — caller decides what silence means.
    """
    payload = _strip_code_fence(raw)
    candidates = _parse_line_delimited(payload)
    if not candidates:
        candidates = _parse_json_fallback(payload)
    return candidates


def select_candidate(raw: str, strategy: str) -> str:
    """Pick one candidate line synchronously (max_prob / top_k_random).

    For ``"judge"`` selection see ``PersonaAgent._judge_select`` — that path
    needs an LLM round-trip and lives on the agent so it can reuse the
    persona's history and the chat context for transcript anchoring.

    Never returns the raw model output: on total parse failure we return
    ``""`` so the caller pipes silence to TTS instead of making the avatar
    recite the format envelope out loud (the 80-second JSON-soliloquy bug).
    """
    candidates = parse_candidates(raw)
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
# Judge selection support — second LLM round-trip lives on PersonaAgent
# because it needs persona history + chat context. The prompt envelope and
# response decoder live here so they're grep-able alongside the rest of the
# sampling code.
# ---------------------------------------------------------------------------


JUDGE_SYSTEM = (
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


def extract_transcript_block(chat_ctx: llm.ChatContext) -> str:
    """Pull the LATEST TRANSCRIPT block out of the most recent user message."""
    for item in reversed(chat_ctx.items):
        if not isinstance(item, llm.ChatMessage) or item.role != "user":
            continue
        text = item.text_content or ""
        m = _TRANSCRIPT_BLOCK_RE.search(text)
        if m:
            return m.group(1).strip()
    return ""


def parse_judge_winner(raw: str, n_candidates: int) -> int:
    """Parse the judge's JSON reply into a 0-based index, or ``-1`` on failure."""
    payload = _strip_code_fence(raw)
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
