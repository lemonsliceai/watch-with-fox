"""Comedic variation angles for Fox.

Each angle is the *name* of a comedic lens defined in the system prompt
(see ``fox_configs/default.py`` SYSTEM_PROMPT). The orchestrator picks
one Fox hasn't used recently and injects it as [LENS: name] into the
per-turn prompt — the LLM looks up the definition from the system prompt.
This rotation is the single biggest lever for joke variety.
"""

import random

from podcast_commentary.agent.fox_config import CONFIG

COMMENTARY_ANGLES: list[str] = list(CONFIG.persona.comedic_angles)


def pick_angle(recent_angles: list[str]) -> str:
    """Pick a commentary lens that wasn't used in the last few comments.

    The number of recent angles to avoid is configured by
    ``persona.angle_lookback``. If that ever makes the pool empty (more
    angles excluded than available), fall back to the full bank.
    """
    lookback = CONFIG.persona.angle_lookback
    avoid = set(recent_angles[-lookback:])
    available = [a for a in COMMENTARY_ANGLES if a not in avoid]
    if not available:
        available = COMMENTARY_ANGLES
    return random.choice(available)
