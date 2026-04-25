# Server

Python 3.11+ backend with two processes: an HTTP API server and a LiveKit AI agent.

The Chrome extension is the only frontend. It captures the active tab's audio via `chrome.tabCapture` and publishes it as a LiveKit track named `podcast-audio`. The agent subscribes to that track for STT. The name `podcast-audio` is internal and predates the Couchverse rebrand; it still carries arbitrary tab audio (videos, podcasts, livestreams, songs).

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
├── api/           # FastAPI HTTP server (sessions, tokens)
│   ├── app.py     # App factory, CORS, lifespan (pool warmup + migrations)
│   └── routes/sessions.py  # Session create / get / end + /health
├── agent/         # LiveKit agent (STT → LLM → TTS → avatar)
│   ├── main.py    # Entrypoint, wires STT/LLM/TTS/VAD plugins
│   ├── comedian.py        # ComedianAgent: phase state machine, commentary orchestration
│   ├── commentary.py      # CommentaryTimer, FullTranscript, timing constants
│   ├── podcast_pipeline.py # Subscribes to podcast-audio track + Groq STT
│   ├── speech_gate.py     # Gate logic for "is Fox speaking?"
│   ├── prompts.py         # System prompts and context builders
│   └── angles.py          # 7 comedic angle definitions (Silicon Valley style)
└── core/          # Shared across api and agent
    ├── config.py  # Pydantic BaseSettings (loads .env)
    └── db.py      # asyncpg pool, migrations, CRUD
```

## Agent phase state machine

Each PersonaAgent uses a `FoxPhase` enum: INTRO → LISTENING → COMMENTATING. Illegal transitions log errors. Always respect the phase model when modifying agent behavior.

## Timing constants (commentary.py)

- `MIN_GAP = 5s` — minimum silence between Fox finishing and starting next comment
- `BURST_WINDOW = 60s`, `BURST_MAX = 8` — max 8 comments per minute
- `BURST_COOLDOWN = 8s` — forced pause after hitting burst limit
- `POST_SPEECH_DELAY = 7s` — wait after podcast speech ends before evaluating timers

## Gotchas

- **Fire-and-forget tasks:** Never use bare `asyncio.create_task()`. Use `_fire_and_forget()` which attaches a done-callback to surface exceptions.
- **Speech handle timeouts:** LemonSlice's *second* avatar in a multi-avatar room is unreliable about sending `lk.playback_finished` back — see livekit/agents #3510 and #4315 (running >1 `AvatarSession` in one room is explicitly unsupported). `SpeechHandle.wait_for_playout` will hang forever when the RPC is missing. Always wait via `Director._wait_for_playout_robust`, which falls through to `PersonaAgent.synthesize_playout_complete()` on timeout — that calls `AudioOutput.on_playback_finished(pushed_duration, interrupted=False)` ourselves, waking the waiter without cutting off audio mid-sentence. `force_listening()` remains only as the nuclear last resort.
- **Database migrations** run inline in the FastAPI lifespan hook via `run_migrations()` in db.py. All DDL is idempotent (`CREATE TABLE IF NOT EXISTS`).
- **Deployment configs** (`fly.toml`, `livekit.toml`) are gitignored. Copy from `.example` files and fill in your values.

## Code style

- Ruff: line-length 100
- Full type annotations using Python 3.11+ `X | Y` union syntax
- `logging.getLogger(__name__)` per module
- snake_case functions/variables, UPPER_CASE constants
