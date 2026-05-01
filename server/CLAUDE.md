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
├── api/           # FastAPI HTTP server (sessions, tokens, agent dispatch)
│   ├── app.py             # App factory, CORS, lifespan (pool warmup + migrations)
│   ├── livekit_tokens.py  # User + agent JWT minting
│   ├── livekit_dispatch.py # RoomAgentDispatch metadata schema (one room per persona)
│   └── routes/sessions.py # POST/GET/PATCH /api/sessions + /health
├── agent/         # LiveKit agent (STT → LLM → TTS → avatar), one AgentSession per persona
│   ├── main.py            # Entrypoint; parses dispatch metadata, builds personas
│   ├── director.py        # Wires components, owns lifecycle (start/shutdown)
│   ├── comedian.py        # PersonaAgent: phase state machine, per-persona delivery
│   ├── commentary.py      # CommentaryTimer, FullTranscript, timing constants
│   ├── commentary_pipeline.py  # Single-flight selector → delivery turn
│   ├── commentary_scheduler.py # Silence loop, watchdog, post-intro kickoff, sentence trigger
│   ├── intro_sequencer.py # Sequenced intros gated on per-persona avatar readiness
│   ├── secondary_room.py  # Self-join wrapper for non-primary persona rooms
│   ├── dispatch_metadata.py # Helper: read dispatch metadata + build persona descriptors
│   ├── control_channel.py # commentary.control I/O (publish + dispatch, fan-out across rooms)
│   ├── playout_waiter.py  # Bounded wait on SpeechHandle.wait_for_playout
│   ├── room_state.py      # Shared mutable state: shutdown flag, last-turn clock, listening predicate
│   ├── settings_controller.py # Frequency / length presets (chattiness, reply length)
│   ├── skip_coordinator.py # Skip-button → interrupt + commentary_end fan-out
│   ├── task_supervisor.py # fire_and_forget tracking + bulk cancel
│   ├── selector.py        # Picks which persona speaks next; consecutive-turn cap
│   ├── podcast_pipeline.py # Subscribes to podcast-audio track + Groq STT
│   ├── speech_gate.py     # Gate logic for "is the agent currently speaking?"
│   ├── prompts.py         # System prompts and context builders
│   ├── angles.py          # Comedic angle definitions
│   ├── metrics.py         # In-process counters (turn totals, RPC outcomes, gaps)
│   └── fox_config.py      # Persona configs (voice, avatar URL, label)
└── core/          # Shared across api and agent
    ├── config.py  # Pydantic BaseSettings (loads .env)
    └── db.py      # asyncpg pool, migrations, CRUD
```

## Agent phase state machine

Each PersonaAgent uses a `FoxPhase` enum: INTRO → LISTENING → COMMENTATING. Illegal transitions log errors. Always respect the phase model when modifying agent behavior.

## Timing constants (commentary.py)

- `MIN_GAP = 5s` — minimum silence between a persona finishing and starting next comment
- `BURST_WINDOW = 60s`, `BURST_MAX = 8` — max 8 comments per minute
- `BURST_COOLDOWN = 8s` — forced pause after hitting burst limit
- `POST_SPEECH_DELAY = 7s` — wait after podcast speech ends before evaluating timers

## Gotchas

- **Fire-and-forget tasks:** Never use bare `asyncio.create_task()`. Use `TaskSupervisor.fire_and_forget()` (in `agent/task_supervisor.py`), which tracks the task for bulk cancel on shutdown and attaches a done-callback that surfaces exceptions. The one deliberate exception is `Director._trip_shutdown_latch` — see the comment there.
- **One `AvatarSession` per room:** every persona runs in its own `rtc.Room` (`{session_id}-{persona}`) so each room hosts exactly one LemonSlice `AvatarSession`. This is what removed the second-avatar `lk.playback_finished` RPC drops we used to see — running >1 `AvatarSession` in one room is explicitly unsupported (livekit/agents #3510, #4315). `PlayoutWaiter` now just `asyncio.wait_for`s `SpeechHandle.wait_for_playout()` against a hard upper bound; the old robust-fallback ladder is gone. Don't reintroduce two `AvatarSession`s in a single room.
- **Database schema** is created on first boot by `ensure_schema()` in db.py, called from the FastAPI lifespan hook. It's `CREATE TABLE IF NOT EXISTS` only — there is no migration ladder. If the schema needs to evolve post-launch, switch to alembic rather than re-introducing ad-hoc `ALTER` calls.
- **Deployment configs** (`fly.toml`, `livekit.toml`) are gitignored. Copy from `.example` files and fill in your values.

## Code style

- Ruff: line-length 100
- Full type annotations using Python 3.11+ `X | Y` union syntax
- `logging.getLogger(__name__)` per module
- snake_case functions/variables, UPPER_CASE constants
