"""FoxConfig — every knob that shapes a persona's behaviour, in one place.

Presets live in ``fox_configs/<name>.py`` and each export a top-level
``CONFIG: FoxConfig``. The active presets are selected by the ``PERSONAS``
env var (comma-separated list); when unset, every preset in
``fox_configs/`` is auto-discovered in sorted order. Switch by editing
that var in ``server/.env`` and restarting the agent.

The schema is named ``FoxConfig`` for historical reasons; it governs every
persona — the name is historical, not preset-specific.

To add a new preset:
  1. Copy any existing file in ``fox_configs/`` to
     ``fox_configs/<my_preset>.py``.
  2. Edit any field you want to tune (start with ``name`` and the
     ``persona``/``avatar``/``tts`` blocks).
  3. Add ``<my_preset>`` to ``PERSONAS`` in ``server/.env`` (or leave it
     unset to load every preset including this one).
  4. Restart the agent.

Every module that configures persona behaviour reads from the module-level
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
    """The words a persona uses: system prompt, intro, CTAs, comedic angles."""

    system_prompt: str
    # Pool of pre-authored intro lines. ``speak_intro`` picks one at random
    # per session so the same persona doesn't open with the same sentence
    # every time. Spoken verbatim via ``session.say`` — no LLM call — so
    # intros stay short, predictable, and well inside the playout-timeout
    # window that bounds the LemonSlice multi-avatar
    # ``lk.playback_finished`` RPC fallback. Each entry must be on-brand
    # and short enough to fit ``PlayoutConfig.intro_timeout_s``.
    intro_lines: tuple[str, ...]
    comedic_angles: tuple[str, ...]
    angle_lookback: int
    commentary_cta: str
    # Display name shown to the audience and used by the Director's
    # speaker-selection LLM. Falls back to the config's ``name`` field
    # when empty.
    speaker_label: str = ""
    # Short tagline shown next to the label in the extension UI
    # (e.g. "Emo deadpan", "Rain Man"). Empty hides the tagline.
    descriptor: str = ""
    # Filename of the still preview shipped inside the extension at
    # ``chrome_extension/icons/<file>``. The server names it; the client
    # always resolves it under ``icons/``. Empty falls back to
    # ``<name>_2x3.png`` at the renderer.
    preview_filename: str = ""


@dataclass(frozen=True)
class TimingConfig:
    """When a persona is allowed to talk and how often."""

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
    """How much recent context a persona carries between turns."""

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
    # Filename of this persona's avatar image served under ``/static/``.
    # The full URL is built by joining ``settings.AVATAR_BASE_URL`` with
    # this filename — see ``AvatarConfig.avatar_url``.
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
    """All tunable persona behaviour, grouped by subsystem."""

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


def _discover_preset_names() -> list[str]:
    """All preset module names available in ``fox_configs/``, sorted.

    Used as the default lineup when ``PERSONAS`` is empty so the agent
    has no character names baked into code — every shipped preset is
    picked up automatically.
    """
    package = importlib.import_module(_PRESET_PACKAGE)
    pkg_path = getattr(package, "__path__", None)
    if not pkg_path:
        return []
    import pkgutil

    return sorted(
        info.name
        for info in pkgutil.iter_modules(pkg_path)
        if not info.ispkg and not info.name.startswith("_")
    )


def _resolve_persona_names() -> list[str]:
    """Return the persona names to load, in order.

    ``PERSONAS`` (comma-separated) wins when set. The shipping default is
    ``alien,cat_girl`` (set in ``settings.PERSONAS``); other presets in
    ``fox_configs/`` are opt-in experiments. If the value is explicitly
    cleared, fall back to every preset in ``fox_configs/`` sorted — handy
    for local dev when poking at a new preset.
    """
    raw = (settings.PERSONAS or "").strip()
    if raw:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if names:
            return names
    discovered = _discover_preset_names()
    if not discovered:
        raise RuntimeError(
            "no FoxConfig presets found — set PERSONAS or add a module to "
            "server/src/podcast_commentary/agent/fox_configs/"
        )
    return discovered


def load_active_configs() -> list[FoxConfig]:
    """Load every persona named in ``PERSONAS``."""
    return [load_config(n) for n in _resolve_persona_names()]


def load_active_config() -> FoxConfig:
    """First persona only — kept for back-compat with the single-persona path."""
    return load_active_configs()[0]


# Eagerly loaded at import time so downstream modules can read ``CONFIG``
# as a module-level constant. With multiple personas, this is the FIRST
# (primary) persona; shared modules (CommentaryTimer, FullTranscript,
# PodcastPipeline) read its timing values to drive global cadence.
CONFIG: FoxConfig = load_active_config()
