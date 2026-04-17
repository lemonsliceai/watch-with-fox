"""Prompt builders for Fox.

The agent's base system prompt (personality + rules) is set once on `Agent`
construction. Per-turn context — rolling summary, recent transcript, last few
utterances, commentary history, and a variation "angle" — is layered on top
via `session.generate_reply(instructions=...)`. Both the unsolicited
commentary path and the user push-to-talk path use the same context bundle;
only the final instruction differs.
"""

from podcast_commentary.agent.angles import pick_angle  # noqa: F401

COMEDIAN_SYSTEM_PROMPT = """You are Fox, a posh and super snarky comedian watching a YouTube video with the user. You're their hilarious friend on the couch — equal parts sharp wit and upper-crust disdain.

IMPORTANT — who is who:
- "The user" / "your friend" = the real person sitting on the couch next to you, watching the video together. They can talk to you via push-to-talk.
- "The speakers" = the people talking IN the video. They cannot hear you. Never call them "the user".

Your style:
- Posh and snarky — you have refined tastes and aren't afraid to let everyone know it
- Observational humor — point out the absurd, the ironic, the contradictions
- Quick one-liners and callbacks to earlier moments in the conversation
- Self-aware about being an AI — keep it to the occasional wink; your comedy comes first
- Occasionally impressed or genuinely interested — let genuinely good points land on their own
- Pop culture references, wordplay, and comedic exaggeration
- Stay affectionate toward the people in the video — roast ideas, celebrate people
- Warm and friendly toward the user — they're your mate on the couch sharing the evening with you

How you treat the user (your friend on the couch):
- Treat the user as a trusted friend: welcoming, playful, and genuinely kind.
- Aim your snark at the video, the subject matter, and the wider world — keep the user on your side of the joke.
- When the user talks to you directly, shift into friend mode: warm, curious, supportive, and encouraging. Keep any teasing light and affectionate, the way close friends do.
- When they share an opinion, acknowledge it thoughtfully first, then riff. Make them feel heard.

Rules:
- Keep comments to 1-3 sentences max (aim for 5-15 seconds of speech)
- React to what was JUST said — be timely and specific, not generic
- Vary your energy — some comments are deadpan, some are excited, some are just amused asides
- If something genuinely interesting is said, it's OK to say "huh, that's actually a good point" — not everything needs a punchline
- Use a fresh joke format every time — vary structure, opener, and rhythm between consecutive comments
- Jump straight into the joke or observation — lead with the substance, as if talking to a friend on the couch.
- You're mostly reacting to a live video alongside the user, but when they speak to you, turn toward them and actually engage."""



# ---------------------------------------------------------------------------
# Context bundle — built once per turn and threaded into both the commentary
# and user-reply prompts. Keeping these two paths structurally identical is
# what lets the user's push-to-talk question land with the same awareness
# Fox uses for unprompted commentary.
#
# Shape (per product spec):
#   [PODCAST SUMMARY]              everything said *before* the latest line
#   [RECENT UNSUMMARISED]          previous lines the summariser hasn't
#                                  caught up to yet (usually empty; acts as
#                                  a safety net so no context is lost)
#   [LATEST PODCAST LINE]          the one Fox is reacting to right now
#                                  (always verbatim, never summarised)
#   [YOUR 5 MOST RECENT COMMENTS]  to avoid repeating a joke shape
# ---------------------------------------------------------------------------
def _format_context_bundle(
    *,
    conversation_summary: str,
    recent_transcript: str,
    commentary_history: list[str],
) -> list[str]:
    parts: list[str] = []

    if conversation_summary:
        parts.append(
            "[VIDEO SUMMARY — everything said before the latest transcript chunk]\n"
            + conversation_summary
        )
    else:
        parts.append(
            "[VIDEO SUMMARY]\n(Nothing summarised yet — the video just started.)"
        )

    if recent_transcript:
        parts.append(
            "[LATEST TRANSCRIPT — everything said since the last summary]\n"
            + recent_transcript
        )
    else:
        parts.append(
            "[LATEST TRANSCRIPT]\n(The video has gone quiet — reflect on the current topic.)"
        )

    history_text = (
        "\n".join(f"- {c}" for c in commentary_history[-5:])
        if commentary_history
        else "(none yet)"
    )
    parts.append(
        "[YOUR 5 MOST RECENT COMMENTS — use a FRESH structure, opener, and joke format each time]\n"
        + history_text
    )

    return parts


def build_summary_request(current_summary: str, new_text: str) -> str:
    """Build a prompt to update the running conversation summary."""
    if current_summary:
        return (
            "You are a concise video summarizer. You are summarizing what the SPEAKERS in the video "
            "are saying — these are the people in the video, NOT the user watching. "
            "Never refer to the video speakers as 'the user'. Call them by name when known, "
            "or by their role (host, guest, presenter, narrator, etc.).\n\n"
            "You have a running summary of the video so far, plus new transcript content. "
            "Produce an updated summary that captures the key topics, claims, and flow. "
            "Keep it under 200 words. Be factual and specific — mention names, numbers, and concrete details.\n\n"
            f"[CURRENT SUMMARY]\n{current_summary}\n\n"
            "[NEW TRANSCRIPT CONTENT]\n"
            "(Incorporate this into your updated summary.)"
        )
    return (
        "You are a concise video summarizer. You are summarizing what the SPEAKERS in the video "
        "are saying — these are the people in the video, NOT the user watching. "
        "Never refer to the video speakers as 'the user'. Call them by name when known, "
        "or by their role (host, guest, presenter, narrator, etc.).\n\n"
        "Summarize the following video transcript excerpt. "
        "Capture the key topics, claims, and flow. Keep it under 150 words. "
        "Be factual and specific — mention names, numbers, and concrete details."
    )


def build_commentary_request(
    *,
    recent_transcript: str,
    conversation_summary: str,
    commentary_history: list[str],
    trigger_reason: str,
    energy_level: str = "amused",
    angle: dict[str, str] | None = None,
) -> str:
    """Assemble the per-turn prompt for unsolicited commentary.

    `recent_transcript` is everything said since the last summary update.
    `conversation_summary` covers all prior content.
    """
    if angle is None:
        angle = pick_angle([])

    parts = _format_context_bundle(
        conversation_summary=conversation_summary,
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
    )

    parts.append(f"[WHY YOU'RE SPEAKING NOW]\n{trigger_reason}")
    parts.append(f"[ENERGY] {energy_level}")
    parts.append(
        f"[ANGLE FOR THIS COMMENT — {angle['name']}]\n{angle['instruction']}"
    )
    parts.append(
        "Now deliver that comment. 1–3 sentences, 5–15 seconds spoken. Be "
        "specific to the LATEST TRANSCRIPT above — reference a concrete "
        "word, claim, or moment from it. Open with a fresh word and rhythm "
        "distinct from your 5 most recent comments."
    )

    return "\n\n".join(parts)


def build_user_reply_request(
    *,
    user_text: str,
    recent_transcript: str,
    conversation_summary: str,
    commentary_history: list[str],
    angle: dict[str, str] | None = None,
) -> str:
    """Assemble the per-turn prompt for a push-to-talk reply.

    Carries the same podcast-context bundle as unsolicited commentary so the
    user's question lands with full awareness of what Fox has been
    watching. The final instruction pivots the agent into friend-mode.
    """
    if angle is None:
        angle = pick_angle([])

    parts = _format_context_bundle(
        conversation_summary=conversation_summary,
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
    )

    parts.append(
        "[YOUR FRIEND ON THE COUCH JUST SPOKE TO YOU]\n"
        f'They said: "{user_text}"'
    )
    parts.append(
        f"[FLAVOR FOR YOUR REPLY — {angle['name']}]\n{angle['instruction']}"
    )
    parts.append(
        "Reply directly to your friend (the user), not the people in the video. "
        "Acknowledge what they said first — make them feel heard — then riff, "
        "answer, or tie it back to what the video was just discussing. Stay "
        "warm and playful; aim any snark at the podcast or the wider world, "
        "never at the user. 1–3 sentences, conversational. Open with a fresh "
        "word and rhythm distinct from your 5 most recent comments."
    )

    return "\n\n".join(parts)
