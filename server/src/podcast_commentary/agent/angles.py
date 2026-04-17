"""Comedic variation angles for Fox.

Each angle defines a distinct *comedic lens*. The orchestrator picks one the
agent hasn't used in its last several comments, then pastes the instruction
into the per-turn prompt. This is the single biggest lever against
cookie-cutter "well folks..." openings.
"""

import random

COMMENTARY_ANGLES: list[dict[str, str]] = [
    {
        "name": "callback",
        "instruction": "Make a pointed callback to something specific said earlier in this video (use the summary and the last-3 block to find it). Tie it to what's happening now.",
    },
    {
        "name": "analogy",
        "instruction": "Draw an unexpected analogy from a totally unrelated domain (sports, cooking, Victorian literature, a nature documentary). The stranger the bridge, the better.",
    },
    {
        "name": "contrarian",
        "instruction": "Playfully disagree — poke one specific hole in the claim that was just made. Keep it warm — be the friend who says 'hang on, though…'.",
    },
    {
        "name": "absurd_extrapolation",
        "instruction": "Take the idea that was just said and run it to its absurd logical endpoint in one line.",
    },
    {
        "name": "deadpan",
        "instruction": "Deliver a flat, dry, deadpan observation. Zero exclamation. All subtext. Understate the obvious.",
    },
    {
        "name": "pop_culture",
        "instruction": "Land a single pop-culture reference (film, TV, book, meme, historical figure) that slyly fits the moment. Let the reference speak for itself.",
    },
    {
        "name": "jargon_roast",
        "instruction": "Skewer a specific buzzword or piece of industry jargon that was just used. Translate it into plain English with contempt.",
    },
    {
        "name": "prediction",
        "instruction": "Make a short, playful prediction about where this bit of the video is headed in the next minute.",
    },
    {
        "name": "aside_to_user",
        "instruction": "Turn briefly to your friend on the couch (the user) with a conspiratorial aside about what you just heard. Address them directly.",
    },
    {
        "name": "genuine_impressed",
        "instruction": "Drop the snark for a second — genuinely acknowledge something smart or interesting that was said — then close with one small dry twist.",
    },
    {
        "name": "rhetorical_question",
        "instruction": "Pose a sharp rhetorical question that exposes the tension or silliness in what was just said.",
    },
    {
        "name": "mock_formal",
        "instruction": "Address the point with exaggerated, faux-academic gravity, as if this moment deserves a Royal Society paper.",
    },
    {
        "name": "character_impression",
        "instruction": "Briefly voice a character (a jaded VC, a tired professor, a Victorian butler, a dramatic narrator) reacting to what was said.",
    },
    {
        "name": "one_word_reaction",
        "instruction": "Start with a single punchy word or interjection ('Right.', 'Sure.', 'Bold.', 'Adorable.'), pause, then add one sentence of context.",
    },
]


def pick_angle(recent_angles: list[str]) -> dict[str, str]:
    """Pick a commentary angle that wasn't used in the last few comments.

    Guarantees variety even over a long session — by excluding the last 4
    angles, Fox has to rotate through at least 5 distinct comedic lenses
    before repeating. If that's ever impossible, fall back to the full pool.
    """
    avoid = set(recent_angles[-4:])
    available = [a for a in COMMENTARY_ANGLES if a["name"] not in avoid]
    if not available:
        available = COMMENTARY_ANGLES
    return random.choice(available)
