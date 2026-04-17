# Server

Python 3.11+ backend with two processes: an HTTP API server and a LiveKit AI agent.

## Commands

```bash
uv sync                          # install deps (reads uv.lock)
uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload  # API
uv run python src/podcast_commentary/agent/main.py dev      # agent (local)
uv run python src/podcast_commentary/agent/main.py start    # agent (production)
uv run ruff check src/           # lint
uv run ruff format --check src/  # format check
uv run pytest                    # test
```

## Structure

```
src/podcast_commentary/
├── api/           # FastAPI HTTP server (sessions, tokens, audio proxy)
│   ├── app.py     # App factory, CORS, lifespan (pool warmup + migrations)
│   └── routes/sessions.py  # All endpoints
├── agent/         # LiveKit agent (STT → LLM → TTS → avatar)
│   ├── main.py    # Entrypoint, wires STT/LLM/TTS/VAD plugins
│   ├── comedian.py        # ComedianAgent: phase state machine, commentary orchestration
│   ├── commentary.py      # CommentaryTimer, FullTranscript, timing constants
│   ├── podcast_pipeline.py # ffmpeg + Groq STT stream + VAD
│   ├── podcast_player.py  # ffmpeg subprocess (16kHz mono PCM)
│   ├── speech_gate.py     # Gate logic for "is Fox speaking?"
│   ├── user_turn.py       # Hold-to-talk state machine with grace timer
│   ├── prompts.py         # System prompts and context builders
│   └── angles.py          # 14 comedic angle definitions
└── core/          # Shared across api and agent
    ├── config.py  # Pydantic BaseSettings (loads .env)
    ├── db.py      # asyncpg pool, migrations, CRUD
    └── youtube.py # yt-dlp audio extraction + bot detection workarounds
```

## Agent phase state machine

ComedianAgent uses a `FoxPhase` enum: INTRO → LISTENING → COMMENTATING → USER_TALKING → REPLYING. Illegal transitions log errors. Always respect the phase model when modifying agent behavior.

## Timing constants (commentary.py)

- `MIN_GAP = 5s` — minimum silence between Fox finishing and starting next comment
- `BURST_WINDOW = 60s`, `BURST_MAX = 8` — max 8 comments per minute
- `BURST_COOLDOWN = 8s` — forced pause after hitting burst limit
- `POST_SPEECH_DELAY = 7s` — wait after podcast speech ends before evaluating timers

## Gotchas

- **yt-dlp YouTube bot detection (2026):** Use `player_client=["default"]` — the `web`/`web_safari` clients have broken URL extraction, `tv` has DRM experiments. The `[default]` extra in pyproject.toml is required for JS challenge-solver bundles.
- **Fire-and-forget tasks:** Never use bare `asyncio.create_task()`. Use `_fire_and_forget()` which attaches a done-callback to surface exceptions.
- **Speech handle timeouts:** LemonSlice avatar can hang on `playback_finished` RPC. Always use `INTRO_PLAYOUT_TIMEOUT` (15s) and `COMMENTARY_PLAYOUT_TIMEOUT` (20s) when waiting on speech handles.
- **Database migrations** run inline in the FastAPI lifespan hook via `run_migrations()` in db.py. All DDL is idempotent (`CREATE TABLE IF NOT EXISTS`).
- **Deployment configs** (`fly.toml`, `livekit.toml`) are gitignored. Copy from `.example` files and fill in your values.

## Code style

- Ruff: line-length 100
- Full type annotations using Python 3.11+ `X | Y` union syntax
- `logging.getLogger(__name__)` per module
- snake_case functions/variables, UPPER_CASE constants
