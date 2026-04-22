# Watch with Fox

AI comedian avatar that delivers real-time comedic commentary while users watch YouTube videos. MST3K meets AI.

## Stack

- **Frontend:** Chrome extension (`chrome_extension/`) — captures YouTube tab audio via `chrome.tabCapture` and publishes it to LiveKit
- **API Server:** FastAPI on Fly.io, asyncpg + Neon PostgreSQL
- **AI Agent:** LiveKit Agents framework on LiveKit Cloud (Groq STT/LLM, ElevenLabs TTS, LemonSlice avatar)

## Running locally

Two server terminals + the loaded extension. They connect: `extension → LiveKit Cloud ← agent`, with the extension also calling `api` to mint tokens.

```bash
# One-time setup
cd server && uv sync && uv run python src/podcast_commentary/agent/main.py download-files
cd ../chrome_extension && npm install && npm run build

# Terminal 1: API (port 8080)
cd server && uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2: Agent
cd server && uv run python src/podcast_commentary/agent/main.py dev

# Load extension: chrome://extensions → Developer mode → Load unpacked → chrome_extension/
```

See [`chrome_extension/README.md`](chrome_extension/README.md) and [`server/README.md`](server/README.md) for details.

## How audio flows

1. Content script monitors the YouTube `<video>` element for play/pause/seek
2. Side panel connects to LiveKit and captures tab audio via `chrome.tabCapture`
3. Tab audio is published as a `podcast-audio` LiveKit track
4. Agent subscribes to this track for STT
5. Avatar + commentary render in the side panel

## Key architecture decisions

- **Agent name isolation:** `AGENT_NAME` in `server/.env` must differ between local and production. Local uses `podcast-commentary-agent-local`; production uses `podcast-commentary-agent`. If both register the same name, LiveKit round-robins between them.
- **Database is optional:** If `DATABASE_URL` is unset, the app runs without persistence. Conversation logging silently no-ops.
- **Avatar URL must be public:** LemonSlice Cloud fetches the avatar image from its servers, so `localhost` URLs won't work. Use the deployed Fly.io URL or an ngrok tunnel.

## Environment

All env vars go in `server/.env` (see `server/.env.example`). The Chrome extension reads its API URL from a setting in the side panel (defaults to `http://localhost:8080` when loaded unpacked).

## Code style

- **Python:** Ruff, line-length 100. Full type annotations (Python 3.11+ union syntax).
- **JavaScript (extension):** Plain ES modules bundled via esbuild.
- Comments explain "why", not "what".
