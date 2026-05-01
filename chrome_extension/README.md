<div align="center">

# Couchverse — Chrome Extension

Chrome MV3 extension that adds two AI co-hosts — **Alien** and **Cat girl** — to a side panel while you watch or listen in the browser.

[![Chrome 116+](https://img.shields.io/badge/Chrome-116%2B-4285F4?logo=googlechrome&logoColor=white)](https://developer.chrome.com/docs/extensions/mv3/intro/)
[![Manifest V3](https://img.shields.io/badge/Manifest-V3-34A853)](https://developer.chrome.com/docs/extensions/mv3/intro/)
[![Node 18+](https://img.shields.io/badge/Node-18%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![esbuild](https://img.shields.io/badge/bundler-esbuild-FFCF00)](https://esbuild.github.io/)
[![LiveKit client](https://img.shields.io/badge/LiveKit-client-FF5722)](https://github.com/livekit/client-sdk-js)

[↑ Back to root README](../README.md)

</div>

---

The extension captures tab audio directly via `chrome.tabCapture` and publishes it to LiveKit, so the agent never needs to extract or decode audio server-side.

## Table of contents

- [Prerequisites](#prerequisites)
- [Getting started](#getting-started)
  - [1. Build the extension](#1-build-the-extension)
  - [2. Server setup](#2-server-setup)
  - [3. Load the extension in Chrome](#3-load-the-extension-in-chrome)
  - [4. Use it](#4-use-it)
- [Configuring the API URL](#configuring-the-api-url)
- [How it works](#how-it-works)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [File structure](#file-structure)
- [Development](#development)

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| [Node.js](https://nodejs.org/) | 18+ | Required by esbuild |
| [uv](https://docs.astral.sh/uv/) | latest | Python package manager |
| [Python](https://www.python.org/) | 3.11+ | For the server |
| Chrome | 116+ | Needed for the Side Panel API |

You'll also need API keys for the [server services](#2-server-setup).

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

The extension needs two server processes running locally: the **API server** (sessions + token issuance) and the **agent worker** (the AI hosts).

<details>
<summary><b>Install server dependencies</b></summary>

```bash
cd server
uv sync
uv run python src/podcast_commentary/agent/main.py download-files
```

</details>

<details>
<summary><b>Configure environment (<code>server/.env</code>)</b></summary>

```bash
cp .env.example .env
```

Fill in:

```env
# Required. Get these from their respective dashboards.
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
GROQ_API_KEY=your-groq-api-key
ELEVEN_API_KEY=your-elevenlabs-api-key
LEMONSLICE_API_KEY=your-lemonslice-api-key

# Agent name. Must differ from production to avoid dispatch collisions.
AGENT_NAME=podcast-commentary-agent-local

# Public base URL hosting the avatar images under /static/<filename>.
# Independent of the API — by default the FastAPI server serves these
# itself, so this is usually your API's public URL, but it can be any
# public host (CDN, S3, GitHub Pages, ngrok, etc.). Leave unset to run
# without avatars.
AVATAR_BASE_URL=https://your-api.fly.dev

# Optional. Leave blank to run without persistence.
DATABASE_URL=
```

> [!IMPORTANT]
> `AVATAR_BASE_URL` must be reachable from LemonSlice Cloud's servers — `localhost` won't work. Either deploy the server, host the avatars on a public CDN/bucket, or expose your local server with ngrok: `ngrok http 8080`.

</details>

<details>
<summary><b>Start the server (two terminals)</b></summary>

```bash
# Terminal 1: API server (port 8080)
cd server
uv run uvicorn podcast_commentary.api.app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2: Agent worker
cd server
uv run python src/podcast_commentary/agent/main.py dev
```

Verify:

```bash
curl http://localhost:8080/health
# → {"status":"ok"}
```

</details>

### 3. Load the extension in Chrome

1. Open `chrome://extensions`.
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select the `chrome_extension/` directory.
4. The Couchverse extension appears in your toolbar.

### 4. Use it

1. Open any tab that's playing audio — video, podcast, song, livestream.
2. Click the Couchverse icon. The side panel opens.
3. The extension auto-detects the tab's URL and title.
4. Click **Start Couchverse**.
5. Alien and Cat girl appear and start reacting.
6. Adjust volume sliders as needed.

## Configuring the API URL

The extension talks to **one** API server, baked into the bundle at build time from the `API_URL` env var. There is no runtime toggle — every build embeds exactly one URL, which is what whoever installs that build gets.

| Goal | Setup |
|---|---|
| Use the hosted Couchverse API (default) | No `.env` needed. `npm run build` bundles `https://podcast-commentary-api.fly.dev`. |
| Local backend development | Set `API_URL=http://localhost:8080` in `chrome_extension/.env`, rebuild, reload the unpacked extension. |
| Self-host on your own deployment | Set `API_URL` to your deployed server, then build and zip. |

`chrome_extension/.env` is gitignored (covered by the root `.gitignore`); `chrome_extension/.env.example` is the template. You can also pass the URL inline for one-off builds:

```bash
API_URL=http://localhost:8080 npm run build
```

> [!TIP]
> The build logs the embedded URL every run (`Build complete: ... (API_URL=...)`), so you can confirm what got bundled before reloading.

## How it works

```
Tab audio ──→ chrome.tabCapture ──→ LiveKit room ──→ Agent (Groq STT)
                                                            │
                                                      Groq LLM (Llama)
                                                            │
                                                      ElevenLabs TTS
                                                            │
                                  Side panel ←── LiveKit ←── LemonSlice avatar
                                (avatars + captions)
```

1. **Content script** (`content.js`) injects into pages and monitors the primary `<video>` or `<audio>` element for play/pause/seek events.
2. **Side panel** (`sidepanel.html`) connects to LiveKit and captures the tab's audio via `chrome.tabCapture`.
3. Tab audio is published to the LiveKit room as a track named `podcast-audio`.
4. The **agent** subscribes and feeds it to Groq Whisper STT.
5. STT transcripts trigger the comedy pipeline: Groq LLM generates a line, ElevenLabs TTS voices it, LemonSlice renders the avatar.
6. Avatar video and audio stream back through LiveKit to the side panel.

## Testing

<details>
<summary><b>Verify the API accepts session creation</b></summary>

```bash
curl -X POST http://localhost:8080/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

Expect a JSON response with `session_id`, `token`, `livekit_url`, and related fields.

</details>

<details>
<summary><b>Verify the agent picks up the audio track</b></summary>

After starting a session from the extension, the agent terminal should log:

```
Podcast pipeline initialised (awaiting podcast-audio track from extension)
```

Once the extension publishes tab audio:

```
Attached podcast-audio track to STT pipeline
First podcast audio frame pushed to STT buffer
```

</details>

<details>
<summary><b>Verify end-to-end</b></summary>

1. Start both server terminals (API and agent).
2. Load the extension in Chrome.
3. Open a tab playing audio.
4. Click the extension icon. Side panel opens.
5. Click **Start Couchverse**.

Confirm:

- Side panel shows "Live" status (green dot).
- Persona status bar shows "Listening" with a headphone emoji.
- Agent logs show `Podcast pipeline initialised ...`.
- After 10–20 seconds, the hosts deliver the first commentary.
- Speech bubbles appear over each avatar.
- Host voices play through the side panel.

</details>

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Session creation failed" | API server not running | Start the API: `uv run uvicorn ...` |
| Side panel shows "Connecting" forever | LiveKit credentials wrong or agent not running | Check `LIVEKIT_URL` / `KEY` / `SECRET` in `.env`, start the agent |
| Persona avatar doesn't appear | `AVATAR_BASE_URL` not reachable from LemonSlice | Use a public URL or ngrok tunnel |
| No commentary after 30 s | Tab audio not reaching agent | Check agent logs for "podcast audio frame" messages |
| "Failed to capture tab audio" | Chrome permission issue | Make sure the target tab is the active tab when clicking start |

## File structure

```
chrome_extension/
├── manifest.json        # MV3 manifest (tabCapture, sidePanel, content scripts)
├── background.js        # Service worker: tab capture, side panel management
├── content.js           # Monitors the page's primary <video> or <audio> element
├── sidepanel.html       # Side panel UI shell
├── styles.css           # Couchverse HUD theme (warm dark, game-like)
├── src/
│   └── sidepanel.js     # Main logic: LiveKit, audio capture, avatars, controls
├── dist/
│   └── sidepanel.js     # Bundled output (built by esbuild)
├── icons/               # Extension icons (persona preview avatars)
├── build.js             # esbuild bundler config
└── package.json         # Dependencies: livekit-client, esbuild
```

## Development

After changes to `src/sidepanel.js`, rebuild:

```bash
npm run build
```

Then reload the extension in `chrome://extensions` (refresh icon on the card).

For auto-rebuild during development:

```bash
npm run watch
```

> [!NOTE]
> You still have to manually reload the extension in Chrome after each rebuild. Changes to `content.js`, `background.js`, `styles.css`, or `sidepanel.html` also require a reload.
