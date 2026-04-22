/**
 * Side panel — LiveKit connection, tab audio capture, avatar rendering, controls.
 *
 * This is the main entry point for the Chrome extension's UI logic. It's
 * bundled by esbuild into dist/sidepanel.js (which sidepanel.html loads).
 *
 * Audio flow:
 *   YouTube tab audio → chrome.tabCapture → MediaStream → LiveKit track
 *   → Agent subscribes → Groq STT → Commentary generation
 */

import {
  Room,
  RoomEvent,
  Track,
  ConnectionState,
} from "livekit-client";

// ── DOM helpers ──
const $ = (sel) => document.querySelector(sel);

// ── API URL defaults ──
// Unpacked/dev installs default to localhost so anyone cloning this repo
// can run the whole stack locally without editing code. Chrome Web Store
// installs default to the hosted production API. An explicit user override
// in chrome.storage.local wins over both.
//
// If you fork this project and publish your own build to the Web Store,
// change PROD_API_URL to point at your deployed API.
const LOCAL_API_URL = "http://localhost:8080";
const PROD_API_URL = "https://watch-with-fox.fly.dev";

function isStoreInstall() {
  // `update_url` is injected into the manifest automatically for extensions
  // installed from the Chrome Web Store. It's absent for unpacked/dev loads.
  return "update_url" in chrome.runtime.getManifest();
}

function getDefaultApiUrl() {
  return isStoreInstall() ? PROD_API_URL : LOCAL_API_URL;
}

// ── State ──
let room = null;
let activeTabId = null;
let tabAudioStream = null;
let tabAudioContext = null;
let tabAudioGain = null;
let ducking = false;
let captions = [];

// ── Init ──
document.addEventListener("DOMContentLoaded", async () => {
  // Pick the default API URL based on install type, then let any explicit
  // user override replace it.
  const defaultApiUrl = getDefaultApiUrl();
  const stored = await chrome.storage.local.get("apiUrl");
  $("#api-url").value = stored.apiUrl || defaultApiUrl;
  $("#api-url").placeholder = defaultApiUrl;

  // Save API URL on change — or clear the override if the user blanks it
  // out or retypes the current default.
  $("#api-url").addEventListener("change", () => {
    const value = $("#api-url").value.trim();
    if (!value || value === getDefaultApiUrl()) {
      chrome.storage.local.remove("apiUrl");
      $("#api-url").value = getDefaultApiUrl();
    } else {
      chrome.storage.local.set({ apiUrl: value });
    }
  });

  // Detect active YouTube tab
  detectYouTubeTab();

  // Wire up controls
  $("#start-btn").addEventListener("click", startSession);
  $("#end-btn").addEventListener("click", endSession);

  // Listen for content script messages relayed through background
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "yt-state-update") {
      handleYouTubeStateUpdate(msg);
    }
    if (msg.type === "yt-video-info") {
      updateVideoPreview(msg);
    }
  });
});

// ── YouTube Tab Detection ──
// Detection never blocks on the content script — if the YouTube tab was
// open before the extension was (re)loaded, the content script was never
// injected into it and `chrome.tabs.sendMessage` would fail silently,
// leaving the UI stuck on "Detecting video...". Instead, derive everything
// we need for the setup screen (videoId, title, URL) directly from the
// tab's own metadata, which is always available via `activeTab`.
//
// The content script is still useful for runtime events (play/pause/seek
// monitoring during a session), so if it isn't responding we inject it
// programmatically via chrome.scripting. A later info message from the
// freshly-injected script will arrive via onMessage and refine the title.
async function detectYouTubeTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab) return;

  activeTabId = tab.id;

  if (!tab.url || !tab.url.includes("youtube.com/watch")) {
    showNoVideoState();
    return;
  }

  // Primary source: parse the tab's own metadata. Works immediately
  // regardless of content script injection state.
  const videoId = extractVideoIdFromUrl(tab.url);
  const title = (tab.title || "").replace(/ - YouTube$/i, "").trim();

  if (videoId) {
    updateVideoPreview({ url: tab.url, videoId, title });
  } else {
    showNoVideoState();
    return;
  }

  // Secondary: ping the content script. If it replies, great. If it
  // doesn't (ReceiverError), inject it so play/pause monitoring works
  // once the session starts.
  try {
    const info = await chrome.tabs.sendMessage(tab.id, { type: "get-video-info" });
    if (info) updateVideoPreview(info);
  } catch {
    console.log("[ext] Content script not present, injecting...");
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
      // Newly injected script will push a yt-video-info message shortly.
    } catch (err) {
      console.warn("[ext] Content script injection failed:", err);
    }
  }
}

function extractVideoIdFromUrl(url) {
  try {
    return new URL(url).searchParams.get("v") || "";
  } catch {
    return "";
  }
}

function showNoVideoState() {
  $("#video-title").textContent = "Open a YouTube video in this tab";
  $("#start-btn").disabled = true;
}

function updateVideoPreview(info) {
  if (info.title) {
    $("#video-title").textContent = info.title;
  }
  if (info.url) {
    $("#start-btn").disabled = false;
    $("#start-btn").dataset.videoUrl = info.url;
    $("#start-btn").dataset.videoTitle = info.title || "";
  }
}

// ── Session Lifecycle ──
async function startSession() {
  const btn = $("#start-btn");
  const videoUrl = btn.dataset.videoUrl;
  const videoTitle = btn.dataset.videoTitle || "";
  const apiUrl = $("#api-url").value.trim();

  if (!videoUrl) {
    showError("No YouTube video detected");
    return;
  }

  btn.disabled = true;
  btn.classList.add("loading");
  btn.textContent = "Starting...";
  hideError();

  try {
    // 1. Create session via API
    const session = await createSessionApi(apiUrl, videoUrl, videoTitle);

    // 2. Show session screen
    $("#setup-screen").classList.add("hidden");
    $("#session-screen").classList.remove("hidden");
    updateStatus("connecting");
    setFoxMood("Connecting...");

    // 3. Connect to LiveKit
    await connectRoom(session.token, session.livekit_url);
    updateStatus("connected");
    setFoxMood("Listening");

    // 4. Capture and publish tab audio
    await captureAndPublishTabAudio();

    console.log("[ext] Session started:", session.session_id);
  } catch (err) {
    console.error("[ext] Failed to start session:", err);
    showError(err.message);
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "Watch with Fox";
    $("#setup-screen").classList.remove("hidden");
    $("#session-screen").classList.add("hidden");
  }
}

async function endSession() {
  updateStatus("disconnected");
  if (room) {
    room.disconnect();
    room = null;
  }
  teardownTabAudio();
  if (unduckTimer) {
    clearTimeout(unduckTimer);
    unduckTimer = null;
  }
  ducking = false;
  captions = [];
  renderCaptions();

  // Return to setup screen
  $("#session-screen").classList.add("hidden");
  $("#setup-screen").classList.remove("hidden");
  const btn = $("#start-btn");
  btn.disabled = false;
  btn.classList.remove("loading");
  btn.textContent = "Watch with Fox";

  // Re-detect video
  detectYouTubeTab();
}

// ── API ──
async function createSessionApi(apiUrl, videoUrl, videoTitle) {
  const res = await fetch(`${apiUrl}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      video_url: videoUrl,
      video_title: videoTitle,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`Session creation failed: ${text}`);
  }
  return res.json();
}

// ── LiveKit Room ──
async function connectRoom(token, livekitUrl) {
  room = new Room({
    adaptiveStream: true,
    dynacast: true,
  });

  room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);
  room.on(RoomEvent.TrackUnsubscribed, onTrackUnsubscribed);
  room.on(RoomEvent.DataReceived, onDataReceived);
  room.on(RoomEvent.ActiveSpeakersChanged, onActiveSpeakers);
  room.on(RoomEvent.ConnectionStateChanged, onConnectionState);
  room.on(RoomEvent.Disconnected, onDisconnected);

  await room.connect(livekitUrl, token);
  console.log("[ext] Connected to LiveKit room");
}

// ── Tab Audio Capture ──
async function captureAndPublishTabAudio() {
  // 1. Request stream ID from background service worker
  const response = await new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      { type: "capture-tab-audio", tabId: activeTabId },
      (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!resp || resp.error) {
          reject(new Error(resp?.error || "Failed to capture tab audio"));
          return;
        }
        resolve(resp);
      }
    );
  });

  // 2. Get MediaStream from the stream ID
  tabAudioStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: response.streamId,
      },
    },
  });

  const audioTracks = tabAudioStream.getAudioTracks();
  if (audioTracks.length === 0) {
    throw new Error("No audio tracks in tab capture stream");
  }

  // 3. Route the captured audio back to the user's speakers.
  //
  // chrome.tabCapture intercepts the tab's audio output — without this
  // loopback the YouTube video would appear to mute the moment we start
  // capturing. Piping through an AudioContext to `destination` plays the
  // same audio the agent receives back out through the local speakers.
  // The gain node is used solely for ducking while Fox is talking; at rest
  // it stays at 1.0 so the user's own YouTube / system volume is preserved.
  tabAudioContext = new AudioContext();
  // Side panels are usually activated by a user gesture, but some Chromium
  // builds still create the context in "suspended" state. Explicit resume
  // makes the loopback audible immediately.
  if (tabAudioContext.state === "suspended") {
    await tabAudioContext.resume();
  }
  const source = tabAudioContext.createMediaStreamSource(tabAudioStream);
  tabAudioGain = tabAudioContext.createGain();
  source.connect(tabAudioGain);
  tabAudioGain.connect(tabAudioContext.destination);

  // 4. Publish the tab audio track to LiveKit.
  //
  // `Source.ScreenShareAudio` is the semantically correct source for
  // captured tab/window audio. Using it (instead of Unknown) ensures
  // LiveKit auto-subscribe works reliably — the agent's room-level
  // track_subscribed handler then matches on `name === "podcast-audio"`
  // and attaches it to the STT pipeline.
  const publication = await room.localParticipant.publishTrack(audioTracks[0], {
    name: "podcast-audio",
    source: Track.Source.ScreenShareAudio,
  });

  console.log(
    "[ext] Published podcast-audio:",
    "sid=", publication?.trackSid,
    "kind=", publication?.kind,
    "source=", publication?.source,
    "muted=", audioTracks[0].muted,
    "readyState=", audioTracks[0].readyState,
  );

  // If the track goes muted / ends unexpectedly, surface it. This helps
  // diagnose cases where tabCapture succeeds but silently stops producing
  // audio (e.g. user switched tabs or the tab was closed).
  audioTracks[0].addEventListener("mute", () =>
    console.warn("[ext] podcast-audio track muted")
  );
  audioTracks[0].addEventListener("ended", () =>
    console.warn("[ext] podcast-audio track ended")
  );
}

function teardownTabAudio() {
  if (tabAudioContext) {
    try { tabAudioContext.close(); } catch {}
    tabAudioContext = null;
    tabAudioGain = null;
  }
  if (tabAudioStream) {
    tabAudioStream.getTracks().forEach((t) => t.stop());
    tabAudioStream = null;
  }
}

// ── LiveKit Event Handlers ──
function onTrackSubscribed(track, publication, participant) {
  const isAvatar =
    participant.identity === "lemonslice-avatar-agent" ||
    participant.attributes?.["lk.publish_on_behalf"];

  if (!isAvatar && track.kind === Track.Kind.Audio) {
    // Fox's voice — attach to a hidden audio element
    const el = track.attach();
    $("#audio-container").appendChild(el);
    return;
  }

  if (!isAvatar) return;

  if (track.kind === Track.Kind.Video) {
    const container = $("#avatar-video");
    const el = track.attach();
    el.style.width = "100%";
    el.style.height = "100%";
    el.style.objectFit = "cover";
    el.style.borderRadius = "17px";
    container.innerHTML = "";
    container.appendChild(el);
    $("#avatar-loading").classList.add("hidden");
    $("#avatar-badge").classList.remove("hidden");
    $("#avatar-container").classList.add("breathing");
    setFoxMood("Vibing");
    spawnReaction("eyes");
  }

  if (track.kind === Track.Kind.Audio) {
    const el = track.attach();
    $("#audio-container").appendChild(el);
  }
}

function onTrackUnsubscribed(track) {
  track.detach().forEach((el) => el.remove());
}

function onDataReceived(payload, participant, kind, topic) {
  let msg;
  try {
    msg = JSON.parse(new TextDecoder().decode(payload));
  } catch {
    return;
  }

  // Agent ready handshake — sync current YouTube playhead
  if (topic === "commentary.control" && msg.type === "agent_ready") {
    console.log("[ext] Agent ready — syncing playhead");
    setFoxMood("Listening");
    syncPlayheadToAgent();
    return;
  }

  // Commentary lifecycle — authoritative source for ducking. These bracket
  // a whole utterance, so they don't flicker the way VAD active-speaker
  // events do between words.
  if (topic === "commentary.control" && msg.type === "commentary_start") {
    setFoxMood("Cooking");
    $("#avatar-container").classList.add("speaking");
    spawnReaction("random");
    setDucking(true);
    return;
  }

  if (topic === "commentary.control" && msg.type === "commentary_end") {
    setFoxMood("Listening");
    $("#avatar-container").classList.remove("speaking");
    setDucking(false);
    return;
  }

  // Captions
  if (msg.type === "agent_transcript" || msg.text) {
    const text = msg.text || msg.content;
    if (text) addCaption(text);
  }
}

// VAD-driven active-speaker updates only drive the avatar "speaking" CSS
// class — purely visual, so it's fine if it flickers. Ducking is handled
// by commentary_start/commentary_end data messages (see onDataReceived).
function onActiveSpeakers(speakers) {
  const localId = room?.localParticipant?.identity;
  const remoteSpeaking = speakers.some((p) => p.identity !== localId);

  if (remoteSpeaking) {
    $("#avatar-container").classList.add("speaking");
  } else {
    $("#avatar-container").classList.remove("speaking");
  }
}

function onConnectionState(state) {
  console.log("[ext] Connection state:", state);
  if (state === ConnectionState.Connected) {
    updateStatus("connected");
  } else if (state === ConnectionState.Reconnecting) {
    updateStatus("connecting");
    setFoxMood("Reconnecting...");
  }
}

function onDisconnected(reason) {
  console.log("[ext] Disconnected:", reason);
  updateStatus("disconnected");
  setFoxMood("Disconnected");
}

// ── Playhead Sync ──
async function syncPlayheadToAgent() {
  if (!room || !activeTabId) return;

  try {
    const state = await chrome.tabs.sendMessage(activeTabId, {
      type: "get-video-state",
    });
    if (!state) return;

    const SYNC_FORWARD_SEC = 0.7;
    if (state.playing) {
      await publishControl(
        { type: "play", t: Math.max(0, state.time + SYNC_FORWARD_SEC) },
        "podcast.control"
      );
    } else {
      await publishControl({ type: "pause" }, "podcast.control");
    }
  } catch (err) {
    console.warn("[ext] Failed to sync playhead:", err);
  }
}

function handleYouTubeStateUpdate(msg) {
  if (!room || room.state !== ConnectionState.Connected) return;

  // Relay play/pause events to agent via data channel
  if (msg.playing) {
    publishControl({ type: "play", t: msg.time }, "podcast.control");
  } else {
    publishControl({ type: "pause" }, "podcast.control");
  }
}

// ── Data Channel ──
async function publishControl(payload, topic) {
  if (!room || room.state !== ConnectionState.Connected) return;
  try {
    const encoder = new TextEncoder();
    await room.localParticipant.publishData(
      encoder.encode(JSON.stringify(payload)),
      { reliable: true, topic }
    );
  } catch (err) {
    console.warn("[ext] publishData failed:", err);
  }
}

// ── Ducking ──
// When Fox speaks, drop the video to a low but still-audible level rather
// than muting. Around -12 dB (25%) is the standard range for dialog ducking;
// going much lower makes the transitions feel dramatic and pump-y. At rest
// the gain is 1.0 — we never modify the user's own YouTube/system volume.
const DUCK_GAIN = 0.25;
const PASSTHROUGH_GAIN = 1.0;

// Release hold on un-duck. Prevents brief gaps (late commentary_end, dropped
// packets) from punching the video back up mid-utterance. 600ms is the
// conventional sweet spot for speech ducking.
const UNDUCK_RELEASE_MS = 600;

// Exponential-ramp time constants for the gain node (seconds). Fast attack
// so Fox isn't stepped on, slower release so the recovery is inaudible.
// setTargetAtTime uses these as the 63%-of-target time constant.
const DUCK_ATTACK_TAU = 0.05;
const DUCK_RELEASE_TAU = 0.25;

let unduckTimer = null;

// Single entry point for toggling the ducking state. On un-duck we hold for
// UNDUCK_RELEASE_MS before actually releasing, to ride over any short gaps.
function setDucking(active) {
  if (active) {
    if (unduckTimer) {
      clearTimeout(unduckTimer);
      unduckTimer = null;
    }
    if (!ducking) {
      ducking = true;
      applyDucking();
    }
    return;
  }
  if (unduckTimer) return;
  unduckTimer = setTimeout(() => {
    unduckTimer = null;
    ducking = false;
    applyDucking();
  }, UNDUCK_RELEASE_MS);
}

function applyDucking() {
  if (!tabAudioGain || !tabAudioContext) return;
  // Ramp the gain exponentially instead of snapping — an instantaneous
  // .value = x is audible as a click/pump; setTargetAtTime fades smoothly
  // with no zipper noise.
  const now = tabAudioContext.currentTime;
  const target = ducking ? DUCK_GAIN : PASSTHROUGH_GAIN;
  const tau = ducking ? DUCK_ATTACK_TAU : DUCK_RELEASE_TAU;
  tabAudioGain.gain.cancelScheduledValues(now);
  tabAudioGain.gain.setTargetAtTime(target, now, tau);
}

// ── Captions (Speech Bubbles) ──
function addCaption(text) {
  captions.push(text);
  if (captions.length > 4) captions = captions.slice(-4);
  renderCaptions();
}

function renderCaptions() {
  const container = $("#captions");
  container.innerHTML = captions
    .map((c) => `<div class="speech-bubble">${escapeHtml(c)}</div>`)
    .join("");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ── Floating Reactions ──
const REACTION_SETS = {
  laugh: ["\u{1F602}", "\u{1F923}", "\u{1F606}", "\u{1F60F}"],
  love:  ["\u{2764}\u{FE0F}", "\u{1F9E1}", "\u{1F525}"],
  eyes:  ["\u{1F440}", "\u{2728}", "\u{1F98A}"],
  fire:  ["\u{1F525}", "\u{1F4A5}", "\u{26A1}"],
};

function spawnReaction(type) {
  const container = $("#reactions");
  if (!container) return;

  // Pick a random set if type is "random"
  const sets = Object.keys(REACTION_SETS);
  const key = type === "random" ? sets[Math.floor(Math.random() * sets.length)] : type;
  const emojis = REACTION_SETS[key] || REACTION_SETS.laugh;
  const emoji = emojis[Math.floor(Math.random() * emojis.length)];

  const particle = document.createElement("span");
  particle.className = "reaction-particle";
  particle.textContent = emoji;
  // Random horizontal drift
  const drift = (Math.random() - 0.5) * 40;
  particle.style.setProperty("--drift", `${drift}px`);
  particle.style.animationDelay = `${Math.random() * 0.2}s`;

  container.appendChild(particle);

  // Clean up after animation
  setTimeout(() => particle.remove(), 2000);
}

// ── Fox Status / Mood ──
const MOOD_ICONS = {
  "Joining...":      "\u{1F98A}",
  "Connecting...":   "\u{1F50C}",
  "Listening":       "\u{1F3A7}",
  "Vibing":          "\u{1F60E}",
  "Cooking":         "\u{1F525}",
  "Talking":         "\u{1F4AC}",
  "Thinking...":     "\u{1F4AD}",
  "Reconnecting...": "\u{1F504}",
  "Disconnected":    "\u{1F634}",
};

function setFoxMood(mood) {
  const moodEl = $("#fox-mood");
  const iconEl = $("#fox-status-icon");
  if (moodEl) moodEl.textContent = mood;
  if (iconEl) iconEl.textContent = MOOD_ICONS[mood] || "\u{1F98A}";
}

// ── UI Helpers ──
function updateStatus(state) {
  const el = $("#status");
  el.className = `status-dot ${state}`;
  const labels = {
    connected: "Live",
    connecting: "Connecting",
    disconnected: "Offline",
  };
  $("#status-text").textContent = labels[state] || state;
}

function showError(msg) {
  const el = $("#setup-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function hideError() {
  $("#setup-error").classList.add("hidden");
}
