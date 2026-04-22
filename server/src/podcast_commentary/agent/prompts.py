"""Prompt builders for Fox.

System prompt (personality + rules) is set once on Agent construction.
Per-turn context (transcript, history, angle) is assembled by
build_commentary_request / build_user_reply_request and passed as the
user_input to generate_reply.

All prompt text is sourced from the active FoxConfig preset — see
``fox_config.py`` and ``fox_configs/default.py``.
"""

from podcast_commentary.agent.angles import pick_angle  # noqa: F401
from podcast_commentary.agent.fox_config import CONFIG

# Re-exported so callers can ``from prompts import COMEDIAN_SYSTEM_PROMPT``
# without reaching into the config object.
COMEDIAN_SYSTEM_PROMPT = CONFIG.persona.system_prompt

# Sentinel that ``comedian.llm_node`` scans for to decide whether to buffer
# the full LLM response and parse candidates. Persona-neutral so any preset
# that enables verbalized sampling gets selection for free.
SAMPLING_SENTINEL = "[[VS_CANDIDATES]]"


def _sampling_instruction() -> str | None:
    """Pipeline-level output-format directive appended when VS is enabled.

    Persona-neutral: never says "joke" or "punchline" — each preset's own
    system prompt + CTA decide what a ``line`` is. Returns None when VS
    is off so the block is omitted from the prompt entirely.
    """
    n = CONFIG.sampling.num_candidates
    if n <= 1:
        return None
    return (
        f"{SAMPLING_SENTINEL}\n"
        f"[OUTPUT FORMAT — pipeline spec, not creative direction]\n"
        f"Return strict JSON only — no prose, no markdown fences: "
        f'{{"candidates":[{{"line":"...","p":0.0}}]}}\n'
        f"Produce exactly {n} candidates. Each `line` is a complete response "
        f"written to the rules above. `p` is your own confidence (0.0-1.0) "
        f"that this candidate lands best. Stay in character across all of them."
    )


def _format_context_bundle(
    *,
    recent_transcript: str,
    commentary_history: list[str],
) -> list[str]:
    parts: list[str] = []

    if recent_transcript:
        parts.append("[LATEST TRANSCRIPT — what the speakers just said]\n" + recent_transcript)
    else:
        parts.append(
            "[LATEST TRANSCRIPT]\n(The video has gone quiet — reflect on the current topic.)"
        )

    shown = CONFIG.context.comments_shown_in_prompt
    history_text = (
        "\n".join(f"- {c}" for c in commentary_history[-shown:])
        if commentary_history
        else "(none yet)"
    )
    parts.append(
        "[YOUR RECENT COMMENTS — use a FRESH structure, opener, and joke format each time]\n"
        + history_text
    )

    return parts


def build_commentary_request(
    *,
    recent_transcript: str,
    commentary_history: list[str],
    trigger_reason: str,
    energy_level: str = "amused",
    angle: str | None = None,
) -> str:
    """Assemble the per-turn prompt for unsolicited commentary."""
    if angle is None:
        angle = pick_angle([])

    parts = _format_context_bundle(
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
    )

    parts.append(f"[WHY YOU'RE SPEAKING NOW]\n{trigger_reason}")
    parts.append(f"[ENERGY] {energy_level}")
    parts.append(f"[LENS: {angle}]")
    parts.append(CONFIG.persona.commentary_cta)

    sampling = _sampling_instruction()
    if sampling:
        parts.append(sampling)

    return "\n\n".join(parts)


def build_user_reply_request(
    *,
    user_text: str,
    recent_transcript: str,
    commentary_history: list[str],
    angle: str | None = None,
) -> str:
    """Assemble the per-turn prompt for a push-to-talk reply."""
    if angle is None:
        angle = pick_angle([])

    parts = _format_context_bundle(
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
    )

    parts.append(f'[YOUR FRIEND ON THE COUCH JUST SPOKE TO YOU]\nThey said: "{user_text}"')
    parts.append(f"[LENS: {angle}]")
    parts.append(CONFIG.persona.user_reply_cta)

    sampling = _sampling_instruction()
    if sampling:
        parts.append(sampling)

    return "\n\n".join(parts)
