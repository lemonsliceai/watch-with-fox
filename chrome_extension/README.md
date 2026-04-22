# Watch with Fox — Chrome Extension

Chrome extension that adds an AI comedian avatar (Fox) as a side panel companion while you watch YouTube. Fox listens to the video's audio, reacts in real time, and delivers comedic commentary.

The extension captures tab audio directly via `chrome.tabCapture` and publishes it to LiveKit, so the agent never needs to extract or decode YouTube audio server-side.

## Prerequisites

- **Node.js** 18+ and npm
- **Python** 3.11+ and [uv](https://docs.astral.sh/uv/)
- **Chrome** 116+ (for Side Panel API support)
- API keys for the server services (see [Server setup](#2-server-setup) below)

## Getting started

### 1. Build the extension

```bash
cd chrome_extension
npm install
npm run build      # bundles src/sidepanel.js → dist/sidepanel.js
```

For development with auto-rebuild on changes:

```bash
npm run watch
```

### 2. Server setup

The extension needs two server processes running locally: the **API server** (creates sessions, issues LiveKit tokens) and the **agent worker** (runs the AI comedian).

#### Install dependencies

```bash
cd server
uv sync
uv run python src/podcast_commentary/agent/main.py download-files
```

#### Configure environment

Copy the example env and fill in your API keys:

```bash
cp .env.example .env
```

Edit `server/.env`:

```env
# Required — get these from their respective dashboards
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
GROQ_API_KEY=your-groq-api-key
ELEVEN_API_KEY=your-elevenlabs-api-key
LEMONSLICE_API_KEY=your-lemonslice-api-key

# Agent name — must differ from production to avoid dispatch collisions
AGENT_NAME=podcast-commentary-agent-local

# Avatar image URL — must be publicly reachable (not localhost)
# Use your deployed server URL or an ngrok tunnel:
#   AVATAR_URL=https://your-app.fly.dev/static/fox_2x3.jpg
#   AVATAR_URL=https://abc123.ngrok.io/static/fox_2x3.jpg
AVATAR_URL=http://localhost:8080/static/fox_2x3.jpg

# Optional — leave blank to run without persistence
DATABASE_URL=
```

> **Note:** `AVATAR_URL` must be reachable from LemonSlice Cloud's servers. `localhost` won't work. Either use a deployed URL or expose your local server with ngrok (`ngrok http 8080`).

#### Start the server (two terminals)

```bash
# Terminal 1: API server (port 8080)
cd server
uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2: Agent worker
cd server
uv run python src/podcast_commentary/agent/main.py dev
```

Verify the API is running:

```bash
curl http://localhost:8080/health
# → {"status":"ok"}
```

### 3. Load the extension in Chrome

1. Open `chrome://extensions`
2. Enable **Developer mode** (toggle in top-right)
3. Click **Load unpacked**
4. Select the `chrome_extension/` directory
5. The "Watch with Fox" extension appears in your toolbar

### 4. Use it

1. Navigate to any YouTube video (e.g. `youtube.com/watch?v=...`)
2. Click the **Watch with Fox** extension icon in the toolbar — the side panel opens
3. The extension auto-detects the video URL and thumbnail
4. The API URL field defaults to `http://localhost:8080` when the extension is loaded unpacked, so no change is needed for local development
5. Click **Watch with Fox** to start
6. Fox's avatar appears in the side panel and begins listening + commenting
7. Use **Hold to talk** to speak to Fox directly
8. Adjust **Video** and **Fox** volume sliders as needed

### API URL — local vs. production

The side panel picks its default API URL based on how the extension was installed:

| Install type | Default API URL | Use case |
|---|---|---|
| **Unpacked** (`Load unpacked` in `chrome://extensions`) | `http://localhost:8080` | Local development — anyone who clones this repo gets a working local setup out of the box |
| **Chrome Web Store** | `https://watch-with-fox.fly.dev` | Normal users installing the published extension |

Detection uses `chrome.runtime.getManifest().update_url`, which the Chrome Web Store injects automatically at install time and is absent for unpacked loads.

You can override the default by editing the **API URL** field on the setup screen — the value is persisted in `chrome.storage.local`. Clearing the field (or retyping the default) removes the override and reverts to the install-type default.

If you fork this project and publish your own build to the Chrome Web Store, update `PROD_API_URL` in `src/sidepanel.js` to point at your deployed API before building.

## How it works

```
YouTube tab audio ──→ chrome.tabCapture ──→ LiveKit room ──→ Agent (Groq STT)
                                                                    │
                                                              Groq LLM (Llama)
                                                                    │
                                                              ElevenLabs TTS
                                                                    │
                                          Side panel ←── LiveKit ←── LemonSlice avatar
                                        (avatar + captions)
```

1. **Content script** (`content.js`) injects into YouTube pages and monitors the `<video>` element for play/pause/seek events
2. **Side panel** (`sidepanel.html`) connects to LiveKit and captures the tab's audio via `chrome.tabCapture`
3. Tab audio is published to the LiveKit room as a track named `podcast-audio`
4. The **agent** subscribes to this track and feeds it to Groq Whisper STT
5. STT transcripts trigger the comedy pipeline: Groq LLM generates a line → ElevenLabs TTS voices it → LemonSlice renders the avatar
6. The avatar video + audio stream back through LiveKit to the side panel

## Testing

### Verify the API accepts session creation

```bash
curl -X POST http://localhost:8080/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

You should get back a JSON response with `session_id`, `token`, `livekit_url`, etc.

### Verify the agent picks up the audio track

After starting a session from the extension, check the agent terminal for:

```
Podcast pipeline initialised (awaiting podcast-audio track from extension)
```

Once the extension publishes the tab audio track:

```
Attached podcast-audio track to STT pipeline
First podcast audio frame pushed to STT buffer
```

### Verify end-to-end

1. Start both server terminals (API + agent)
2. Load the extension in Chrome
3. Open a YouTube video
4. Click the extension icon → side panel opens
5. Click "Watch with Fox"
6. Confirm:
   - Side panel shows "Live" status (green dot)
   - Fox status bar shows "Listening" with headphone emoji
   - Agent logs show `Podcast pipeline initialised (awaiting podcast-audio track from extension)`
   - After 10-20 seconds, Fox should deliver the first commentary
   - Speech bubbles appear in the side panel over the avatar
   - Fox's voice plays through the side panel

### Common issues

| Symptom | Cause | Fix |
|---|---|---|
| "Session creation failed" | API server not running | Start the API: `uv run uvicorn ...` |
| Side panel shows "Connecting" forever | LiveKit credentials wrong or agent not running | Check `LIVEKIT_URL`/`KEY`/`SECRET` in `.env`, start the agent |
| Fox avatar doesn't appear | `AVATAR_URL` not reachable from LemonSlice | Use a public URL or ngrok tunnel |
| No commentary after 30s | Tab audio not reaching agent | Check agent logs for "podcast audio frame" messages |
| "Failed to capture tab audio" | Chrome permission issue | Make sure the YouTube tab is the active tab when clicking start |

## File structure

```
chrome_extension/
├── manifest.json        # MV3 manifest (tabCapture, sidePanel, content scripts)
├── background.js        # Service worker: tab capture, side panel management
├── content.js           # YouTube page: monitors <video> play/pause/seek
├── sidepanel.html       # Side panel UI shell
├── styles.css           # Cozy Companion HUD theme (warm dark, game-like)
├── src/
│   └── sidepanel.js     # Main logic: LiveKit, audio capture, avatar, controls
├── dist/
│   └── sidepanel.js     # Bundled output (built by esbuild)
├── icons/               # Extension icons (generated from fox avatar)
├── build.js             # esbuild bundler config
├── package.json         # Dependencies: livekit-client, esbuild
└── README.md            # This file
```

## Development

After making changes to `src/sidepanel.js`, rebuild:

```bash
npm run build
```

Then reload the extension in `chrome://extensions` (click the refresh icon on the extension card).

For auto-rebuild during development:

```bash
npm run watch
```

You still need to manually reload the extension in Chrome after each rebuild. Changes to `content.js`, `background.js`, `styles.css`, or `sidepanel.html` also require a reload.
