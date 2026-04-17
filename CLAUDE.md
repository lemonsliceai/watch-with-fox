# Watch with Fox

AI comedian avatar that delivers real-time comedic commentary while users watch YouTube videos. MST3K meets AI.

## Stack

- **Frontend:** Next.js 16, React 19, TypeScript, Tailwind CSS v4, LiveKit client
- **API Server:** FastAPI on Fly.io, asyncpg + Neon PostgreSQL
- **AI Agent:** LiveKit Agents framework on LiveKit Cloud (Groq STT/LLM, ElevenLabs TTS, LemonSlice avatar)

## Running locally

Three terminals required — they connect: `web → api → LiveKit Cloud ← agent`

```bash
# One-time setup
cd server && uv sync && uv run python src/podcast_commentary/agent/main.py download-files
cd ../web && npm install

# Terminal 1: API (port 8080)
cd server && uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2: Agent
cd server && uv run python src/podcast_commentary/agent/main.py dev

# Terminal 3: Web (port 3000)
cd web && npm run dev
```

## Key architecture decisions

- **Agent name isolation:** `AGENT_NAME` in `server/.env` must differ between local and production. Local uses `podcast-commentary-agent-local`; production uses `podcast-commentary-agent`. If both register the same name, LiveKit round-robins between them.
- **Audio proxy:** YouTube CDN doesn't send CORS headers. The API proxies audio through `GET /api/audio-stream/{id}` so the browser's Web Audio API can capture it.
- **YouTube IP pinning:** YouTube signs audio URLs to the requester's IP. The agent (not the API) must extract the URL via yt-dlp so ffmpeg fetches from the same IP. When using a proxy, sticky sessions pin the exit IP for 30 minutes.
- **Database is optional:** If `DATABASE_URL` is unset, the app runs without persistence. Conversation logging silently no-ops.
- **Avatar URL must be public:** LemonSlice Cloud fetches the avatar image from its servers, so `localhost` URLs won't work. Use the deployed Fly.io URL or an ngrok tunnel.

## Environment

All env vars go in `server/.env` (see `server/.env.example`). The web app only needs `NEXT_PUBLIC_API_URL`, which is baked into the npm dev/prod scripts.

## Code style

- **Python:** Ruff, line-length 100. Full type annotations (Python 3.11+ union syntax).
- **TypeScript:** ESLint with Next.js config. Strict mode. Absolute imports via `@/` alias.
- Comments explain "why", not "what".
