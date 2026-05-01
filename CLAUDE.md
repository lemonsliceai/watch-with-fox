# Couchverse

Two AI hosts (Alien and Cat girl) deliver live comedic commentary on whatever audio is playing in the user's current browser tab. Think MST3K, except the hecklers live in a Chrome side panel and they'll cover a podcast or a TikTok feed as happily as a movie.

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
2. Side panel calls `POST /api/sessions`; the API mints **one LiveKit room per persona** (`{session_id}-{persona}`) and returns one `room_name` + access token per persona, with exactly one marked `role: "primary"`
3. Side panel connects to **every** room (one per persona). Tab audio is captured via `chrome.tabCapture` and published as a `podcast-audio` LiveKit track to the **primary room only** — never to secondary rooms
4. The agent worker is RoomAgentDispatch'd into the primary room and self-joins each secondary room using a server-minted agent JWT carried in the dispatch metadata
5. The agent runs **one `AgentSession` per persona**, each bound to its own `rtc.Room`. STT runs only against the `podcast-audio` track in the primary room (single STT pipeline driving every persona)
6. Each persona's LemonSlice avatar publishes into that persona's room under participant identity `lemonslice-avatar-{persona}` — so every room has at most one `AvatarSession`
7. Avatars and commentary render in the side panel: incoming avatar tracks route to UI slots by identity, and commentary captions/skip events travel on a `commentary.control` data channel that fans out across all rooms (deduped client- and agent-side by event id)

## Room topology

```
                     ┌──────────────────────────────────┐
                     │        Chrome side panel         │
                     │   (joins every persona's room)   │
                     └──┬──────────────────┬────────────┘
        tab audio ─────►│                  │
   (publish to primary  │                  │
    room only)          ▼                  ▼
         ┌──────────────────────────┐  ┌──────────────────────────┐
         │  {session_id}-alien      │  │  {session_id}-cat_girl   │
         │  role: PRIMARY           │  │  role: SECONDARY         │
         ├──────────────────────────┤  ├──────────────────────────┤
         │  podcast-audio  ◄──────   │  │  (no podcast-audio)      │
         │  AgentSession[alien]     │  │  AgentSession[cat_girl]  │
         │  lemonslice-avatar-alien │  │  lemonslice-avatar-      │
         │                          │  │    cat_girl              │
         └────────────┬─────────────┘  └────────────┬─────────────┘
                      ▲                            ▲
                      │ RoomAgentDispatch          │ self-join with
                      │ (one job per session)      │ server-minted agent JWT
                      └──────────────┬─────────────┘
                                     │
                            ┌──────────────────┐
                            │   Agent worker   │
                            │  (one process,   │
                            │   N personas)    │
                            └──────────────────┘
```

Each persona owns exactly one room; each room contains exactly one `AvatarSession`. The `commentary.control` data channel is published into every room (deduped by `event_id`) so each side picks up the union of skip/caption/lifecycle events without double-handling.

Cross-persona awareness is preserved at the **text** layer: every persona's emitted line is broadcast on `commentary.control`, so Alien can call back to Cat girl's last quip even though Cat girl's TTS audio is in a different room.

## Key architecture decisions

- **Agent name isolation:** `AGENT_NAME` in `server/.env` must differ between local and production. Local uses `podcast-commentary-agent-local`; production uses `podcast-commentary-agent`. If both register the same name, LiveKit round-robins between them.
- **One room per persona:** the API mints a deterministic room per persona (`{session_id}-{persona}`) and the agent runs one `AgentSession` per room. This keeps `lemonslice-avatar-{persona}` the only `AvatarSession` in its room and sidesteps the multi-`AvatarSession`-in-one-room RPC collision (livekit/agents #3510 / #4315). Adding a persona means the API emits an extra `RoomEntry` and the worker self-joins one extra secondary room — no per-room code branching.
- **Database is optional:** If `DATABASE_URL` is unset, the app runs without persistence. Conversation logging silently no-ops.
- **Avatar URL must be public:** LemonSlice Cloud fetches the avatar image from its servers, so `localhost` URLs won't work. Use the deployed Fly.io URL or an ngrok tunnel.
- **Internal names lag the brand:** The Python package is still `podcast_commentary` and the LiveKit track is still `podcast-audio`. These are internal identifiers, not user-visible, and renaming them is a dedicated refactor.

## Environment

All env vars go in `server/.env` (see `server/.env.example`). The Chrome extension reads its API URL from a build-time `API_URL` baked into the bundle (defaults to the hosted `https://podcast-commentary-api.fly.dev`; override in `chrome_extension/.env` to `http://localhost:8080` for local backend dev).

## Code style

- **Python:** Ruff, line-length 100. Full type annotations (Python 3.11+ union syntax).
- **JavaScript (extension):** Plain ES modules bundled via esbuild.
- Comments explain "why", not "what".
