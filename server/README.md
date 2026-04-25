<div align="center">

# Couchverse — Server

Python 3.11+ backend: a **FastAPI HTTP server** and a **LiveKit AI agent** in two processes.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LiveKit Agents](https://img.shields.io/badge/LiveKit-Agents-FF5722)](https://docs.livekit.io/agents/)
[![uv](https://img.shields.io/badge/uv-managed-DE5FE9)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64)](https://docs.astral.sh/ruff/)
[![Fly.io](https://img.shields.io/badge/deploy-Fly.io-7B3FE4)](https://fly.io/)

[↑ Back to root README](../README.md) · [Architecture notes](CLAUDE.md)

</div>

---

## Table of contents

- [Quick start](#quick-start)
- [Commands](#commands)
- [FoxConfig — tuning host behaviour](#foxconfig--tuning-host-behaviour)
  - [Layout](#layout)
  - [Schema](#schema)
  - [Switching presets](#switching-presets)
  - [Notes](#notes)

## Quick start

```bash
cp .env.example .env          # fill in API keys
uv sync
uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload   # API
uv run python src/podcast_commentary/agent/main.py dev                              # agent
```

> [!TIP]
> See the repo root [`README.md`](../README.md) for the big picture and [`CLAUDE.md`](CLAUDE.md) for architecture gotchas and code-style conventions.

## Commands

| Task | Command |
|---|---|
| Install dependencies | `uv sync` |
| Run API (hot reload) | `uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload` |
| Run agent (local) | `uv run python src/podcast_commentary/agent/main.py dev` |
| Run agent (prod) | `uv run python src/podcast_commentary/agent/main.py start` |
| Lint | `uv run ruff check src/` |
| Format check | `uv run ruff format --check src/` |
| Tests | `uv run pytest` |
| Deploy agent (prod) | `lk agent deploy` |
| Deploy API (prod) | `fly deploy` |

## FoxConfig — tuning host behaviour

Every knob that shapes a host — the system prompt, comedic angles, response CTAs, timing + cadence, and LLM/STT/TTS/VAD/avatar settings — lives in a single dataclass loaded once per agent process.

> [!NOTE]
> The schema is still named `FoxConfig` for historical reasons; it governs **every** persona, not just the Fox one.

### Layout

```
src/podcast_commentary/agent/
├── fox_config.py              # FoxConfig schema + loader + CONFIG export
└── fox_configs/               # Preset bank, one file per personality
    ├── __init__.py
    ├── fox.py                 # Stock production values (the primary comedian)
    └── chaos_agent.py         # Alien, the chaos co-host
```

### Schema

`fox_config.py` defines the `FoxConfig` dataclass with nine nested sub-configs:

| Sub-config | What it governs |
|---|---|
| `persona` | `system_prompt`, `intro_prompt`, `comedic_angles`, `angle_lookback`, `commentary_cta` |
| `timing` | `min_silence_between_jokes_s`, `burst_window_s`, `max_jokes_per_burst`, `burst_cooldown_s`, `sentences_before_joke`, `silence_fallback_s`, `post_speech_safety_s`, `transcript_chunk_s` |
| `context` | `comment_memory_size`, `comments_shown_in_prompt` |
| `llm` | `model`, `max_tokens` |
| `stt` | `model` |
| `tts` | `voice_id`, `model`, `stability`, `similarity_boost`, `speed` |
| `vad` | `activation_threshold` |
| `avatar` | `active_prompt`, `idle_prompt`, `startup_timeout_s` |
| `playout` | `intro_timeout_s`, `commentary_timeout_s` |

Every module (`prompts.py`, `angles.py`, `commentary.py`, `comedian.py`, `podcast_pipeline.py`, `main.py`) reads from the module-level `CONFIG` — no other file hardcodes behaviour knobs.

### Switching presets

The active presets are selected by the `PERSONAS` env var in `server/.env` (comma-separated, defaults to `fox,chaos_agent`). Each entry must match a filename in `fox_configs/` (without the `.py` extension).

<details>
<summary><b>Creating and testing a new preset</b></summary>

```bash
# 1. Copy fox as a starting point
cp src/podcast_commentary/agent/fox_configs/fox.py \
   src/podcast_commentary/agent/fox_configs/spicy.py

# 2. Edit spicy.py — tweak anything in the FoxConfig(...) block.
#    Be sure to update `name="spicy"` so logs show which preset loaded.

# 3. Point the agent at the new preset (alongside or instead of the defaults)
echo "PERSONAS=spicy,chaos_agent" >> .env

# 4. Restart the agent
uv run python src/podcast_commentary/agent/main.py dev
```

On startup the agent logs each loaded preset:

```
Loaded FoxConfig preset 'spicy'
```

</details>

> [!WARNING]
> If `PERSONAS` contains a name that doesn't match a file in `fox_configs/`, the agent fails fast with a clear error — no silent fallback.

### Notes

- **Frozen dataclasses.** Every sub-config is `@dataclass(frozen=True)` — presets are read-only snapshots so nothing mutates a persona mid-session.
- **Loaded once per process.** `CONFIG` is evaluated at import time. To switch presets, change `PERSONAS` in `.env` and restart the agent; hot-reload is not supported.
- **Keep `fox.py` as ground truth.** When adding new knobs, update the `FoxConfig` schema in `fox_config.py`, add the value to `fox.py`, and reference it from the module that needs it.
- **Don't hardcode new knobs.** If you find yourself about to drop a new magic number or prompt string into a module, add it to `FoxConfig` first.
