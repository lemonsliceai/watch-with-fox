"""FoxConfig — every knob that shapes Fox's behaviour, in one place.

Presets live in ``fox_configs/<name>.py`` and each export a top-level
``CONFIG: FoxConfig``. The active presets are selected by the ``PERSONAS``
env var (comma-separated list, defaults to ``"fox,chaos_agent"``); switch
by editing that var in ``server/.env`` and restarting the agent.

To add a new preset:
  1. Copy ``fox_configs/fox.py`` to ``fox_configs/<my_preset>.py``.
  2. Edit any field you want to tune.
  3. Add ``<my_preset>`` to ``PERSONAS`` in ``server/.env``.
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
    # Static intro line — spoken verbatim via ``session.say`` so intros are
    # short, predictable, and immune to the LemonSlice multi-avatar
    # ``lk.playback_finished`` RPC flakiness that bites the longer
    # LLM-generated intros. Intros should be the most reliable thing in
    # the show — use ``intro_prompt`` for variant presets that can afford
    # to generate.
    intro_line: str
    intro_prompt: str
    comedic_angles: tuple[str, ...]
    angle_lookback: int
    commentary_cta: str
    # Display name shown to the audience and used by the Director's
    # speaker-selection LLM (e.g. "Fox", "Alien"). Falls back to the
    # config's ``name`` field when empty.
    speaker_label: str = ""


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
    # Filename of this persona's avatar image served under ``/static/``
    # (e.g. ``"fox_2x3.jpg"``). The full URL is built by joining
    # ``settings.AVATAR_BASE_URL`` with this filename — see
    # ``AvatarConfig.avatar_url``.
    avatar_image: str = ""

    @property
    def avatar_url(self) -> str:
        """Full URL LemonSlice will fetch. Empty when disabled."""
        base = settings.AVATAR_BASE_URL
        if not base or not self.avatar_image:
            return ""
        return f"{base.rstrip('/')}/static/{self.avatar_image}"


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
    # ``judge``: rerank with a second LLM call against a 3-axis rubric
    # (anchor / fresh / snap). Adds ~1-2s latency for one extra round-trip
    # but fights RLHF mode-collapse on humor better than self-rated
    # confidence — the model knows what's "likely," not what's "funny."
    # On timeout/error, falls back to max_prob.
    selection: Literal["max_prob", "top_k_random", "judge"] = "max_prob"
    # Judge LLM model (Groq). Same default as the comedians — could be
    # swapped for a smaller fast model. Only consulted when ``selection``
    # is ``"judge"``.
    judge_model: str = "llama-3.3-70b-versatile"
    # Hard cap on the judge round-trip. Miss this and we ship the
    # max_prob pick instead — better a slightly-wrong line than dead air.
    judge_timeout_s: float = 2.5


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


def load_config(name: str) -> FoxConfig:
    """Import ``fox_configs.<name>`` and return its ``CONFIG`` export."""
    name = name.strip()
    module_path = f"{_PRESET_PACKAGE}.{name}"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"persona {name!r} does not resolve to a preset — expected "
            f"server/src/podcast_commentary/agent/fox_configs/{name}.py"
        ) from exc

    cfg = getattr(module, "CONFIG", None)
    if not isinstance(cfg, FoxConfig):
        raise RuntimeError(f"{module_path} must export a top-level `CONFIG: FoxConfig`")
    logger.info("Loaded FoxConfig preset %r", cfg.name)
    return cfg


def _resolve_persona_names() -> list[str]:
    """Return the persona names to load, in order, from ``PERSONAS``."""
    raw = (settings.PERSONAS or "fox").strip()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or ["fox"]


def load_active_configs() -> list[FoxConfig]:
    """Load every persona named in ``PERSONAS``."""
    return [load_config(n) for n in _resolve_persona_names()]


def load_active_config() -> FoxConfig:
    """First persona only — kept for back-compat with the single-Fox path."""
    return load_active_configs()[0]


# Eagerly loaded at import time so downstream modules can read ``CONFIG``
# as a module-level constant. With multiple personas, this is the FIRST
# (primary) persona; shared modules (CommentaryTimer, FullTranscript,
# PodcastPipeline) read its timing values to drive global cadence.
CONFIG: FoxConfig = load_active_config()
