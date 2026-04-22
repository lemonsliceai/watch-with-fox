"""FoxConfig — every knob that shapes Fox's behaviour, in one place.

Presets live in ``fox_configs/<name>.py`` and each export a top-level
``CONFIG: FoxConfig``. The active preset is selected by the ``FOX_CONFIG``
env var (defaults to ``"default"``); switch presets by editing that var in
``server/.env`` and restarting the agent.

To add a new preset:
  1. Copy ``fox_configs/default.py`` to ``fox_configs/<my_preset>.py``.
  2. Edit any field you want to tune.
  3. Set ``FOX_CONFIG=<my_preset>`` in ``server/.env``.
  4. Restart the agent.

Every module that configures Fox's behaviour reads from the module-level
``CONFIG`` exported here — no other file should hardcode behaviour knobs.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Literal

from podcast_commentary.core.config import settings

logger = logging.getLogger("podcast-commentary.fox_config")


# ---------------------------------------------------------------------------
# Sub-configs — grouped by the subsystem they govern.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonaConfig:
    """The words Fox uses: system prompt, intro, CTAs, comedic angles."""

    system_prompt: str
    intro_prompt: str
    comedic_angles: tuple[str, ...]
    angle_lookback: int
    commentary_cta: str
    user_reply_cta: str


@dataclass(frozen=True)
class TimingConfig:
    """When Fox is allowed to talk and how often."""

    min_silence_between_jokes_s: float
    burst_window_s: float
    max_jokes_per_burst: int
    burst_cooldown_s: float
    sentences_before_joke: int
    silence_fallback_s: float
    post_speech_safety_s: float
    user_turn_grace_s: float
    transcript_chunk_s: float


@dataclass(frozen=True)
class ContextConfig:
    """How much recent context Fox carries between turns."""

    comment_memory_size: int
    comments_shown_in_prompt: int


@dataclass(frozen=True)
class LLMConfig:
    model: str
    max_tokens: int


@dataclass(frozen=True)
class STTConfig:
    model: str


@dataclass(frozen=True)
class TTSConfig:
    voice_id: str
    model: str
    stability: float
    similarity_boost: float
    speed: float


@dataclass(frozen=True)
class VADConfig:
    activation_threshold: float


@dataclass(frozen=True)
class AvatarConfig:
    active_prompt: str
    idle_prompt: str
    startup_timeout_s: float


@dataclass(frozen=True)
class PlayoutConfig:
    """Safety-net timeouts for avatar speech playout."""

    intro_timeout_s: float
    commentary_timeout_s: float


@dataclass(frozen=True)
class SamplingConfig:
    """Verbalized-sampling controls — advanced output-diversity tuning.

    With ``num_candidates == 1`` (default) the model writes a single line
    and ships it verbatim — current, lowest-latency behaviour.

    With ``num_candidates > 1`` the model generates N candidates with
    self-assigned confidence scores in one shot; ``selection`` picks
    which one reaches TTS. Based on Zhang et al. 2025, "Verbalized
    Sampling" (arXiv 2510.01171) — reports 1.6-2.1x diversity gain on
    creative tasks, fights RLHF mode collapse without temperature hacks.

    This is pipeline-level, persona-agnostic. Each preset opts in by
    setting ``num_candidates`` and scaling ``LLMConfig.max_tokens`` to
    fit N serialised candidates in JSON.
    """

    # 1 = verbalized sampling off (single-response, streamed to TTS).
    # N>1 = request N candidates in one JSON response, pick one via
    # ``selection``. Full response is buffered before TTS starts.
    num_candidates: int = 1
    # ``max_prob``: always ship the highest-confidence candidate.
    # ``top_k_random``: uniform-random pick from top-3 — adds variance,
    # fits personas where predictability is off-brand (e.g. chaos).
    selection: Literal["max_prob", "top_k_random"] = "max_prob"


@dataclass(frozen=True)
class FoxConfig:
    """All tunable Fox behaviour, grouped by subsystem."""

    name: str
    persona: PersonaConfig
    timing: TimingConfig
    context: ContextConfig
    llm: LLMConfig
    stt: STTConfig
    tts: TTSConfig
    vad: VADConfig
    avatar: AvatarConfig
    playout: PlayoutConfig
    sampling: SamplingConfig = SamplingConfig()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_PRESET_PACKAGE = "podcast_commentary.agent.fox_configs"


def load_active_config() -> FoxConfig:
    """Import ``fox_configs.<FOX_CONFIG>`` and return its ``CONFIG`` export."""
    name = (settings.FOX_CONFIG or "default").strip()
    module_path = f"{_PRESET_PACKAGE}.{name}"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"FOX_CONFIG={name!r} does not resolve to a preset — expected "
            f"server/src/podcast_commentary/agent/fox_configs/{name}.py"
        ) from exc

    cfg = getattr(module, "CONFIG", None)
    if not isinstance(cfg, FoxConfig):
        raise RuntimeError(f"{module_path} must export a top-level `CONFIG: FoxConfig`")
    logger.info("Loaded FoxConfig preset %r (FOX_CONFIG=%s)", cfg.name, name)
    return cfg


# Eagerly loaded at import time so downstream modules can read ``CONFIG``
# as a module-level constant. Presets are selected once per process — to
# switch, change FOX_CONFIG in server/.env and restart.
CONFIG: FoxConfig = load_active_config()
