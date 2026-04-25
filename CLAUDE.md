# Couchverse

Two AI hosts (Fox and Alien) deliver live comedic commentary on whatever audio is playing in the user's current browser tab. Think MST3K, except the hecklers live in a Chrome side panel and they'll cover a podcast or a TikTok feed as happily as a movie.

## Stack

- **Frontend:** Chrome extension (`chrome_extension/`), captures tab audio via `chrome.tabCapture` and publishes it to LiveKit
- **API Server:** FastAPI on Fly.io, asyncpg + Neon PostgreSQL
- **AI Agent:** LiveKit Agents framework on LiveKit Cloud (Groq STT/LLM, ElevenLabs TTS, LemonSlice avatar)

## Running locally

Two server terminals plus the loaded extension. They connect: `extension → LiveKit Cloud ← agent`, with the extension also calling `api` to mint tokens.

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

1. Content script monitors the active page's primary `<video>` or `<audio>` element for play/pause/seek
2. Side panel connects to LiveKit and captures tab audio via `chrome.tabCapture`
3. Tab audio is published as a `podcast-audio` LiveKit track
4. Agent subscribes to this track for STT
5. Avatars and commentary render in the side panel

## Key architecture decisions

- **Agent name isolation:** `AGENT_NAME` in `server/.env` must differ between local and production. Local uses `podcast-commentary-agent-local`; production uses `podcast-commentary-agent`. If both register the same name, LiveKit round-robins between them.
- **Database is optional:** If `DATABASE_URL` is unset, the app runs without persistence. Conversation logging silently no-ops.
- **Avatar URL must be public:** LemonSlice Cloud fetches the avatar image from its servers, so `localhost` URLs won't work. Use the deployed Fly.io URL or an ngrok tunnel.
- **Internal names lag the brand:** The Python package is still `podcast_commentary` and the LiveKit track is still `podcast-audio`. These are internal identifiers, not user-visible, and renaming them is a dedicated refactor.

## Environment

All env vars go in `server/.env` (see `server/.env.example`). The Chrome extension reads its API URL from a build-time `API_URL` baked into the bundle (defaults to the hosted `https://podcast-commentary-api.fly.dev`; override in `chrome_extension/.env` to `http://localhost:8080` for local backend dev).

## Code style

- **Python:** Ruff, line-length 100. Full type annotations (Python 3.11+ union syntax).
- **JavaScript (extension):** Plain ES modules bundled via esbuild.
- Comments explain "why", not "what".
