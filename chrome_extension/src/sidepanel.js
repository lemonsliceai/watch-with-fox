/**
 * Side panel — LiveKit connection, tab audio capture, avatar rendering, controls.
 *
 * This is the main entry point for the Chrome extension's UI logic. It's
 * bundled by esbuild into dist/sidepanel.js (which sidepanel.html loads).
 *
 * Audio flow:
 *   Tab audio (any site) → chrome.tabCapture → MediaStream → LiveKit track
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

// ── API URL ──
// Inlined at build time from `API_URL` in chrome_extension/.env (see
// build.js + .env.example). Defaults to the hosted Couchverse API;
// override to http://localhost:8080 for local backend development.
const API_URL = __API_URL__;

// ── State ──
let room = null;
let activeTabId = null;
let tabAudioStream = null;
// One AudioContext owns the whole mixing graph: tab audio flows through
// tabDuckGain → destination, and each avatar voice flows through its own
// personaNode (MediaElementSource → trim GainNode → destination, with an
// AnalyserNode tap). The tap signals drive the sidechain envelope follower
// that controls tabDuckGain — so the tab ducks off the actual voice signal
// rather than off server-sent start/end events. One shared context is
// required so the follower (on the voice side) can influence the tab gain.
let audioCtx = null;
let tabDuckGain = null;
// personaName (or participant.identity fallback) → {
//   source, trimGain, analyser, buffer, audioEl, track
// }. Built lazily in onTrackSubscribed, torn down in onTrackUnsubscribed.
const personaNodes = new Map();
// Guards against a rapid End → Start double-click re-entering the flow
// while the room is still tearing down. `endSession` holds this across
// the full `room.disconnect()` promise; `startSession` refuses to run
// until it clears. Without this, a new session can publish its
// podcast-audio track before the old room's disconnect has reached the
// server, and the agent briefly sees two user participants.
let sessionBusy = false;
// UI-only: which personas are currently mid-utterance. Drives slot
// highlighting and Skip button state. Does NOT drive audio ducking —
// that's signal-driven off the voice analysers, not these events, so a
// late commentary_end from LemonSlice can't leave the tab stuck ducked.
const speakingNow = new Set();
// Per-persona caption history keyed by persona name (e.g. "fox", "chaos_agent").
const captionsByPersona = new Map();
// Currently-connected LemonSlice avatar personas. When this drains to empty
// after at least one has connected, the session auto-ends — there's no point
// staying on the session screen with no comedians left to chime in.
const connectedAvatars = new Set();
let everHadAvatar = false;
// Personas currently mid-intro. Tracked separately from `speakingNow` so the
// Skip button can stay disabled during intros — the intro ritual (Fox, then
// Alien) is non-skippable, and a stray click landing between the two used to
// cut off Alien's intro. Server-side `SkipCoordinator` also rejects skips on
// intro-phase personas; this is the client-side belt-and-suspenders.
const introNow = new Set();

// LemonSlice avatar participants are named lemonslice-avatar-<persona>.
// Routing decisions in onTrackSubscribed / onActiveSpeakers parse the suffix.
const AVATAR_IDENTITY_PREFIX = "lemonslice-avatar-";

// Audio-only personas all publish from the agent's single local_participant,
// so the participant identity can't disambiguate them. Each persona's TTS
// track is named persona-<name> on the server (main.py) so we can.
const PERSONA_TRACK_PREFIX = "persona-";

function personaFromAvatarIdentity(identity) {
  if (!identity || !identity.startsWith(AVATAR_IDENTITY_PREFIX)) return null;
  return identity.slice(AVATAR_IDENTITY_PREFIX.length);
}

function personaFromTrackName(name) {
  if (!name || !name.startsWith(PERSONA_TRACK_PREFIX)) return null;
  return name.slice(PERSONA_TRACK_PREFIX.length);
}

function slotFor(personaName) {
  if (!personaName) return null;
  return document.querySelector(`.avatar-slot[data-name="${personaName}"]`);
}

// ── Init ──
document.addEventListener("DOMContentLoaded", async () => {
  // Detect active media tab
  detectActiveMedia();

  // Wire up controls
  $("#start-btn").addEventListener("click", startSession);
  $("#end-btn").addEventListener("click", endSession);
  $("#skip-btn").addEventListener("click", skipCommentary);
  initPacingControls();

  // Listen for content script messages relayed through background
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "media-state-update") {
      handleMediaStateUpdate(msg);
    }
    if (msg.type === "media-video-info") {
      updateVideoPreview(msg);
    }
  });
});

// ── Active Tab / Media Detection ──
// Detection never blocks on the content script — if the page was open before
// the extension was (re)loaded, the content script was never injected into
// it and `chrome.tabs.sendMessage` would fail silently, leaving the UI stuck
// on "Detecting video...". Instead, derive what we can (URL, title) directly
// from the tab's own metadata, which is always available via `activeTab`.
//
// The content script is still useful for runtime events (play/pause/seek
// monitoring during a session), so if it isn't responding we inject it
// programmatically via chrome.scripting.
async function detectActiveMedia() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab) return;

  activeTabId = tab.id;

  if (!isCapturableTabUrl(tab.url)) {
    showNoVideoState();
    return;
  }

  // Use the tab's own metadata as the immediate preview. Strip common
  // " - Site Name" suffixes for nicer display.
  const title = stripTitleSuffix(tab.title || "") || tab.url;
  updateVideoPreview({ url: tab.url, title });

  // Ping the content script for richer info (and to confirm it's alive).
  // If it doesn't reply, inject it so play/pause monitoring works once the
  // session starts.
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
      // Freshly-injected script will push a media-video-info message shortly.
    } catch (err) {
      console.warn("[ext] Content script injection failed:", err);
    }
  }
}

function isCapturableTabUrl(url) {
  if (!url) return false;
  // chrome://, edge://, about:, file:, view-source: etc. can't be tab-captured.
  return url.startsWith("http://") || url.startsWith("https://");
}

// Trim the trailing " - Site Name" / " | Site Name" / " — Site Name" that
// most sites tack onto <title>. Leaves the leading content (which is almost
// always the actual media title) untouched.
function stripTitleSuffix(title) {
  return title
    .replace(/\s+[-|–—]\s+[^-|–—]+$/, "")
    .trim();
}

function showNoVideoState() {
  $("#start-btn").disabled = true;
  delete $("#start-btn").dataset.videoUrl;
  delete $("#start-btn").dataset.videoTitle;
}

function updateVideoPreview(info) {
  if (info.url) {
    $("#start-btn").disabled = false;
    $("#start-btn").dataset.videoUrl = info.url;
    $("#start-btn").dataset.videoTitle = info.title || "";
  }
}

// ── Session Lifecycle ──
async function startSession() {
  const btn = $("#start-btn");
  // Prevent re-entering mid-teardown: `endSession` holds `sessionBusy`
  // across the room.disconnect promise, so a stray click here during that
  // window would otherwise start a new room before the old one is gone.
  if (sessionBusy) return;
  const videoUrl = btn.dataset.videoUrl;
  const videoTitle = btn.dataset.videoTitle || "";
  const apiUrl = API_URL;

  if (!videoUrl) {
    showError("No active media tab detected");
    return;
  }

  sessionBusy = true;
  btn.disabled = true;
  btn.classList.add("loading");
  btn.textContent = "Starting...";
  hideError();

  try {
    // Build the audio graph now, while we're still synchronously on the
    // Start-button user gesture. AudioContext creation is allowed in any
    // context, but an initial resume() must be tied to a gesture to avoid
    // a "suspended" context that silently drops samples. We also want the
    // graph to exist before the first avatar track can arrive.
    initAudioGraph();

    // 1. Create session via API
    const session = await createSessionApi(apiUrl, videoUrl, videoTitle);

    // 2. Show session screen
    $("#setup-screen").classList.add("hidden");
    $("#session-screen").classList.remove("hidden");

    // 3. Connect to LiveKit
    await connectRoom(session.token, session.livekit_url);

    // 4. Capture and publish tab audio
    await captureAndPublishTabAudio();

    console.log("[ext] Session started:", session.session_id);
  } catch (err) {
    console.error("[ext] Failed to start session:", err);
    showError(err.message);
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "Start Couchverse";
    $("#setup-screen").classList.remove("hidden");
    $("#session-screen").classList.add("hidden");
  } finally {
    sessionBusy = false;
  }
}

async function endSession() {
  if (sessionBusy) return;
  sessionBusy = true;

  const endBtn = $("#end-btn");
  if (endBtn) endBtn.disabled = true;

  try {
    if (room) {
      const prior = room;
      room = null;
      // `disconnect(true)` returns a promise that resolves once the
      // LiveKit transport is actually closed. Awaiting it prevents a
      // new Start from racing with the old room's teardown.
      try {
        await prior.disconnect(true);
      } catch (err) {
        console.warn("[ext] room.disconnect raised:", err);
      }
    }
    teardownAudioGraph();
    speakingNow.clear();
    introNow.clear();
    updateSkipButton();
    connectedAvatars.clear();
    everHadAvatar = false;
    captionsByPersona.clear();
    document
      .querySelectorAll(".avatar-slot .captions")
      .forEach((el) => (el.innerHTML = ""));
    document
      .querySelectorAll(".avatar-slot")
      .forEach((el) => {
        el.classList.remove("speaking", "breathing", "video-live");
        // Drop the live video element so the next session starts from a
        // clean preview-only state.
        const videoContainer = el.querySelector(".avatar-video");
        if (videoContainer) videoContainer.innerHTML = "";
      });

    // Return to setup screen
    $("#session-screen").classList.add("hidden");
    $("#setup-screen").classList.remove("hidden");
    const btn = $("#start-btn");
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "Start Couchverse";

    // Re-detect media in the active tab
    detectActiveMedia();
  } finally {
    if (endBtn) endBtn.disabled = false;
    sessionBusy = false;
  }
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
  room.on(RoomEvent.ParticipantConnected, onParticipantConnected);
  room.on(RoomEvent.ParticipantDisconnected, onParticipantDisconnected);

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

  // 2. Get MediaStream from the stream ID.
  //
  // Disable echoCancellation / noiseSuppression / autoGainControl. getUserMedia
  // turns these on by default, and AGC in particular quietly attenuates loud
  // tab audio to normalize loudness — perceived as a small volume drop the
  // moment capture starts. Turning them off keeps the loopback bit-perfect so
  // tab volume stays put, giving the sidechain duck a stable reference level
  // to ramp down from.
  tabAudioStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: response.streamId,
      },
      optional: [
        { echoCancellation: false },
        { noiseSuppression: false },
        { autoGainControl: false },
      ],
    },
  });

  const audioTracks = tabAudioStream.getAudioTracks();
  if (audioTracks.length === 0) {
    throw new Error("No audio tracks in tab capture stream");
  }

  // 3. Route the captured audio back to the user's speakers.
  //
  // chrome.tabCapture intercepts the tab's audio output — without this
  // loopback the page would appear to mute the moment we start capturing.
  // Route through the shared AudioContext's tabDuckGain so the sidechain
  // envelope follower can drive it based on persona voice energy. At rest
  // tabDuckGain sits at PASSTHROUGH_GAIN so the page's own volume is
  // preserved bit-for-bit.
  if (!audioCtx || !tabDuckGain) {
    // Defensive — initAudioGraph runs before this in startSession, but
    // if something has torn it down concurrently (e.g. End was clicked
    // mid-start), skip the Web Audio hookup and publish the raw stream.
    // The tab will be inaudible locally, but the agent still gets STT.
    console.warn("[ext] Audio graph missing — tab audio loopback skipped");
  } else {
    if (audioCtx.state === "suspended") {
      await audioCtx.resume().catch(() => {});
    }
    const source = audioCtx.createMediaStreamSource(tabAudioStream);
    source.connect(tabDuckGain);
  }

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

// See initAudioGraph / teardownAudioGraph further down — they own the
// lifecycle of the shared AudioContext, the sidechain envelope follower,
// and all per-persona nodes. This module used to have a tab-only variant
// (teardownTabAudio) but the whole graph is now one unit so there's no
// reason to tear half of it down separately.

// ── LiveKit Event Handlers ──
function onTrackSubscribed(track, publication, participant) {
  const avatarPersona = personaFromAvatarIdentity(participant.identity);
  const trackPersona = personaFromTrackName(
    track.name || publication?.trackName,
  );
  const personaName = avatarPersona || trackPersona;
  const isAvatarTrack =
    avatarPersona !== null || participant.attributes?.["lk.publish_on_behalf"];

  // All persona voice audio (avatar + audio-only direct-publish) routes
  // through the shared audio graph so the sidechain envelope follower
  // can see it. Routing key is the persona name when we can derive one
  // (avatar identity or persona-<name> track name), participant identity
  // otherwise — but two audio-only personas share an identity, so the
  // track-name fallback is required to keep them distinct.
  if (track.kind === Track.Kind.Audio) {
    const key = personaName || `id:${participant.identity}`;
    attachPersonaAudio(track, key);
    return;
  }

  if (!isAvatarTrack) return;

  // For avatar tracks we now know which slot they belong to.
  const slot = slotFor(personaName);

  if (track.kind === Track.Kind.Video && slot) {
    const container = slot.querySelector(".avatar-video");
    const el = track.attach();
    el.style.width = "100%";
    el.style.height = "100%";
    el.style.objectFit = "cover";
    el.style.borderRadius = "15px";
    container.innerHTML = "";
    container.appendChild(el);
    // Swap the still preview for the live video. The `video-live` class
    // drives a fade-in on the video + fade-out on the still image so the
    // transition reads as the preview "animating into" the avatar.
    slot.classList.add("video-live", "breathing");
    spawnReaction(slot, "eyes");
  }
}

function onTrackUnsubscribed(track, publication, participant) {
  if (track.kind === Track.Kind.Audio) {
    const personaName =
      personaFromAvatarIdentity(participant.identity) ||
      personaFromTrackName(track.name || publication?.trackName);
    const key = personaName || `id:${participant.identity}`;
    if (personaNodes.has(key)) {
      detachPersonaAudio(key);
      return;
    }
  }
  track.detach().forEach((el) => el.remove());
}

function onDataReceived(payload, participant, kind, topic) {
  let msg;
  try {
    msg = JSON.parse(new TextDecoder().decode(payload));
  } catch {
    return;
  }

  // Agent ready handshake — sync current playhead + push the user's
  // saved pacing preferences so they take effect from the first turn.
  if (topic === "commentary.control" && msg.type === "agent_ready") {
    console.log("[ext] Agent ready — syncing playhead");
    syncPlayheadToAgent();
    publishPacing();
    return;
  }

  // Commentary lifecycle — drives UI state only (slot highlighting, Skip
  // button enable). Tab-audio ducking is NOT driven from here; the
  // sidechain envelope follower watches the actual persona voice signal
  // and decides for itself. That decoupling is deliberate: LemonSlice's
  // second-avatar `lk.playback_finished` RPC is flaky (see
  // server/CLAUDE.md) and a late commentary_end would otherwise leave
  // the tab stuck ducked — the signal-driven duck recovers on its own
  // the moment the voice stops producing audio.
  if (topic === "commentary.control" && msg.type === "commentary_start") {
    const personaName = msg.speaker;
    const phase = msg.phase || "commentary";
    if (personaName) {
      if (phase === "intro") introNow.add(personaName);
      else speakingNow.add(personaName);
      const slot = slotFor(personaName);
      slot?.classList.add("speaking");
      spawnReaction(slot, "random");
    }
    updateSkipButton();
    return;
  }

  if (topic === "commentary.control" && msg.type === "commentary_end") {
    const personaName = msg.speaker;
    const phase = msg.phase || "commentary";
    if (personaName) {
      if (phase === "intro") introNow.delete(personaName);
      else speakingNow.delete(personaName);
      // Only drop the "speaking" class if nobody from either set is
      // still mid-utterance for that persona.
      if (!speakingNow.has(personaName) && !introNow.has(personaName)) {
        slotFor(personaName)?.classList.remove("speaking");
      }
    }
    updateSkipButton();
    return;
  }

  // Captions
  if (msg.type === "agent_transcript" || msg.text) {
    const text = msg.text || msg.content;
    const personaName = msg.speaker || guessSpeakerFromState();
    if (text) addCaption(personaName, text);
  }
}

// Fallback when a transcript message doesn't carry a `speaker` field
// (older agent build). Pick the persona currently mid-utterance, or fall
// back to the first slot if nobody is.
function guessSpeakerFromState() {
  if (speakingNow.size === 1) return speakingNow.values().next().value;
  const first = document.querySelector(".avatar-slot");
  return first?.dataset.name || null;
}

// VAD-driven active-speaker updates highlight the matching slot only
// when commentary.control hasn't already lit it. Purely visual jitter is
// acceptable here; commentary_start/end remains the authoritative source.
function onActiveSpeakers(speakers) {
  const localId = room?.localParticipant?.identity;
  const activePersonas = new Set();
  for (const p of speakers) {
    if (p.identity === localId) continue;
    const personaName = personaFromAvatarIdentity(p.identity);
    if (personaName) activePersonas.add(personaName);
  }
  for (const slot of document.querySelectorAll(".avatar-slot")) {
    const name = slot.dataset.name;
    if (activePersonas.has(name) || speakingNow.has(name)) {
      slot.classList.add("speaking");
    } else {
      slot.classList.remove("speaking");
    }
  }
}

function onConnectionState(state) {
  console.log("[ext] Connection state:", state);
}

function onDisconnected(reason) {
  console.log("[ext] Disconnected:", reason);
}

function onParticipantConnected(participant) {
  const personaName = personaFromAvatarIdentity(participant.identity);
  if (!personaName) return;
  connectedAvatars.add(personaName);
  everHadAvatar = true;
}

// Once every avatar that joined this session has left, there's no commentary
// coming — auto-end so the user lands back on the start screen instead of an
// empty stage. The `everHadAvatar` gate prevents this from firing during the
// initial connect window before any avatar has shown up.
function onParticipantDisconnected(participant) {
  const personaName = personaFromAvatarIdentity(participant.identity);
  if (!personaName) return;
  connectedAvatars.delete(personaName);
  if (everHadAvatar && connectedAvatars.size === 0) {
    endSession();
  }
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

// ── Skip Commentary ──
// Tells the agent to cut off whoever's mid-utterance. Button stays disabled
// unless a commentary (non-intro) turn is in-flight, so a click always
// targets a skippable turn. The agent's SkipCoordinator also rejects skips
// on intro-phase personas; this is client-side enforcement on top. The
// agent answers by interrupting each eligible persona's SpeechHandle, which
// in turn fires `commentary_end` — the normal handler above clears slot
// highlights and un-ducks.
function skipCommentary() {
  if (speakingNow.size === 0) return;
  publishControl({ type: "skip" }, "podcast.control");
}

function updateSkipButton() {
  const btn = $("#skip-btn");
  if (!btn) return;
  // Disabled when nobody is mid-commentary OR when an intro is in-flight —
  // intros are non-skippable so the Fox → Alien ritual always plays out.
  btn.disabled = speakingNow.size === 0 || introNow.size > 0;
}

// ── Pacing controls (Chattiness / Reply length) ──
// Two segmented controls wired through a single handler. Choices persist
// across sessions in localStorage and are re-sent after `agent_ready` so a
// freshly-connected agent picks them up. Before the room connects, clicks
// still update the UI + localStorage — they take effect next session.
const PACING_STORAGE_KEY = "couchverse.pacing";
const PACING_DEFAULTS = { frequency: "normal", length: "normal" };
const pacing = { ...PACING_DEFAULTS };

function initPacingControls() {
  Object.assign(pacing, loadPacing());
  for (const group of document.querySelectorAll(".segmented")) {
    const setting = group.dataset.setting;
    if (!setting) continue;
    syncSegmentedGroup(group, pacing[setting]);
    group.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".seg-btn");
      if (!btn || !group.contains(btn)) return;
      selectPacing(setting, btn.dataset.value);
    });
  }
}

function selectPacing(setting, value) {
  if (!value || pacing[setting] === value) return;
  pacing[setting] = value;
  savePacing();
  const group = document.querySelector(`.segmented[data-setting="${setting}"]`);
  if (group) syncSegmentedGroup(group, value);
  publishPacing();
}

function syncSegmentedGroup(group, activeValue) {
  for (const btn of group.querySelectorAll(".seg-btn")) {
    btn.classList.toggle("is-active", btn.dataset.value === activeValue);
  }
}

function publishPacing() {
  publishControl(
    { type: "settings", frequency: pacing.frequency, length: pacing.length },
    "podcast.control",
  );
}

function loadPacing() {
  try {
    const raw = localStorage.getItem(PACING_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return {
      frequency: parsed.frequency || PACING_DEFAULTS.frequency,
      length: parsed.length || PACING_DEFAULTS.length,
    };
  } catch {
    return {};
  }
}

function savePacing() {
  try {
    localStorage.setItem(PACING_STORAGE_KEY, JSON.stringify(pacing));
  } catch {
    // Private mode / quota — silently ignore; the UI still works per-session.
  }
}

function handleMediaStateUpdate(msg) {
  if (!room || room.state !== ConnectionState.Connected) return;

  // Pausing the media cuts the tab-audio track the agent relies on for STT,
  // so there's nothing left for Fox to react to. End the session rather
  // than leave the avatar streaming into silence.
  if (!msg.playing) {
    endSession();
    return;
  }

  publishControl({ type: "play", t: msg.time }, "podcast.control");
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

// ── Audio graph ──
//
// One AudioContext. Two kinds of inputs routed into it:
//
//   tab audio          → tabDuckGain ──────────────────────────────┐
//                                                                  ▼
//   persona voice #1   → trimGain → ┬→ destination              destination
//                                   └→ analyser (sidechain tap) ┘
//   persona voice #2   → trimGain → ┬→ destination
//                                   └→ analyser (sidechain tap)
//
// A rAF envelope follower reads peak RMS across every persona analyser
// and drives tabDuckGain — tab audio ducks off the *actual* voice signal,
// not off server events. That's the battle-tested sidechain architecture
// every broadcast mixer has used for decades; the Web Audio API doesn't
// expose a native sidechain input (webaudio/web-audio-api#246) so we roll
// our own envelope follower. Key properties this gets us for free:
//
//   * Any persona's voice ducks the tab at the same depth — no per-speaker
//     tuning needed, and adding a third comedian is zero-config.
//   * A persona's own voice CANNOT duck itself — avatar audio is on a
//     different branch from tabDuckGain, by graph construction.
//   * Late or missing commentary_end events from LemonSlice can't leave
//     the tab stuck ducked — when the voice stops producing samples,
//     RMS drops below threshold and the gain releases.
//   * Per-persona trim normalizes ElevenLabs voice-loudness differences
//     (Fanz is softer than Dave) without touching the duck logic.

const PASSTHROUGH_GAIN = 1.0;

// RMS threshold above which we consider the persona to be actively
// speaking. ~0.01 ≈ -40 dB — comfortably above TTS-idle noise floor
// and well below any real speech energy.
const DUCK_RMS_THRESHOLD = 0.01;

// Depth and time constants for the sidechain duck. Attack is short so
// the tab drops before the first syllable is stepped on; release is slow
// enough to ride out breaths without pumping. Hold keeps the duck
// engaged for a beat after the signal drops below threshold — same
// purpose the old UNDUCK_RELEASE_MS served, but here the release is a
// smooth exponential ramp instead of a hard flip.
const DUCK_TARGET_GAIN = 0.15;   // ~-16 dB
const DUCK_ATTACK_TAU = 0.05;    // seconds
const DUCK_RELEASE_TAU = 0.3;    // seconds
const DUCK_HOLD_MS = 500;

// Per-persona output trim. ElevenLabs voices ship at different reference
// loudness; this normalizes them at the client so the mix is balanced.
// Add new personas here as they're introduced.
const PERSONA_TRIM_GAIN = {
  fox: 1.0,          // Dave voice — our reference level
  chaos_agent: 1.6,  // Fanz ships noticeably softer than Dave
};
const DEFAULT_PERSONA_TRIM = 1.0;

// Envelope follower state. lastVoiceActiveMs drives the hold; currentDuckTarget
// lets us skip redundant setTargetAtTime calls when nothing's changing.
let envelopeFollowerHandle = null;
let lastVoiceActiveMs = 0;
let currentDuckTarget = PASSTHROUGH_GAIN;

function initAudioGraph() {
  if (audioCtx) return;
  audioCtx = new AudioContext();
  // Resume is async but must be *initiated* on the user gesture. We
  // don't await — starting is enough; samples will flow as soon as the
  // state transitions out of "suspended".
  if (audioCtx.state === "suspended") {
    audioCtx.resume().catch((err) =>
      console.warn("[ext] audioCtx.resume failed:", err)
    );
  }
  tabDuckGain = audioCtx.createGain();
  tabDuckGain.gain.value = PASSTHROUGH_GAIN;
  tabDuckGain.connect(audioCtx.destination);

  lastVoiceActiveMs = 0;
  currentDuckTarget = PASSTHROUGH_GAIN;
  startEnvelopeFollower();
}

function teardownAudioGraph() {
  stopEnvelopeFollower();
  for (const name of Array.from(personaNodes.keys())) {
    detachPersonaAudio(name);
  }
  if (tabAudioStream) {
    tabAudioStream.getTracks().forEach((t) => t.stop());
    tabAudioStream = null;
  }
  if (audioCtx) {
    try { audioCtx.close(); } catch {}
    audioCtx = null;
    tabDuckGain = null;
  }
}

// Route a persona's voice track through a (source → trim → destination)
// chain with an analyser tap for the sidechain. Idempotent on key —
// calling twice replaces the previous node so late reconnects don't
// leak disconnected graph nodes.
function attachPersonaAudio(track, key) {
  if (personaNodes.has(key)) detachPersonaAudio(key);

  // Chrome's WebRTC pipeline doesn't deliver samples to a
  // MediaStreamAudioSourceNode unless *something* is also consuming the
  // underlying remote track as a media element. When an avatar is
  // attached, its own `<video>` sink provides that consumer. For direct-
  // publish audio-only personas there's no such sink, so the graph runs
  // dry and the user hears silence despite RTP arriving. Attach the track
  // to a hidden audio element with `volume = 0` purely as a wake-up
  // consumer — it never produces sound (volume-zero is reliable on
  // remote tracks where `muted = true` historically wasn't) but it keeps
  // RTP flowing so the source node below actually sees samples.
  const el = track.attach();
  el.volume = 0;
  $("#audio-container").appendChild(el);

  if (!audioCtx) {
    // Extreme-edge fallback: graph not ready. Let the element play
    // directly so the user still hears the comedian; no sidechain input
    // this session. Better than silence.
    el.volume = 1;
    return;
  }

  let source;
  try {
    const mediaStream = new MediaStream([track.mediaStreamTrack]);
    source = audioCtx.createMediaStreamSource(mediaStream);
  } catch (err) {
    console.warn("[ext] createMediaStreamSource failed for", key, err);
    el.volume = 1;
    return;
  }

  const trimGain = audioCtx.createGain();
  trimGain.gain.value = PERSONA_TRIM_GAIN[key] ?? DEFAULT_PERSONA_TRIM;

  const analyser = audioCtx.createAnalyser();
  // 1024 samples ≈ 21ms at 48kHz — a whole syllable fits, so RMS reads
  // smoothly without needing heavy smoothing constants.
  analyser.fftSize = 1024;
  analyser.smoothingTimeConstant = 0.1;
  const buffer = new Float32Array(analyser.fftSize);

  source.connect(trimGain);
  trimGain.connect(audioCtx.destination);
  // Sidechain tap — post-trim so the envelope follower sees
  // user-perceived loudness (same level the listener hears). Post-trim
  // also means a mis-set trim can't leave the duck silently mistuned.
  trimGain.connect(analyser);

  personaNodes.set(key, { source, trimGain, analyser, buffer, audioEl: el, track });
}

function detachPersonaAudio(key) {
  const node = personaNodes.get(key);
  if (!node) return;
  try { node.source.disconnect(); } catch {}
  try { node.trimGain.disconnect(); } catch {}
  try { node.analyser.disconnect(); } catch {}
  try { node.track?.detach(node.audioEl); } catch {}
  node.audioEl?.remove();
  personaNodes.delete(key);
}

// ── Sidechain envelope follower ──
//
// Runs per animation frame. Reads peak RMS across all persona analysers
// (max, not sum — two quiet voices shouldn't duck deeper than one loud
// voice), compares to threshold + hold window, drives tabDuckGain.
//
// rAF is the right scheduler here: the side panel is always visible
// during an active session (closing it ends the session), so we don't
// hit the background-tab throttling that would otherwise be a concern.
// Upgrading to an AudioWorklet would buy us frame-accurate response in
// exchange for a separate worklet module — not worth it yet.
function startEnvelopeFollower() {
  if (envelopeFollowerHandle) return;
  const tick = () => {
    envelopeFollowerHandle = requestAnimationFrame(tick);
    if (!audioCtx || !tabDuckGain) return;

    let peakRms = 0;
    for (const node of personaNodes.values()) {
      node.analyser.getFloatTimeDomainData(node.buffer);
      let sumSquares = 0;
      for (let i = 0; i < node.buffer.length; i++) {
        const s = node.buffer[i];
        sumSquares += s * s;
      }
      const rms = Math.sqrt(sumSquares / node.buffer.length);
      if (rms > peakRms) peakRms = rms;
    }

    const now = performance.now();
    const aboveThreshold = peakRms > DUCK_RMS_THRESHOLD;
    if (aboveThreshold) lastVoiceActiveMs = now;
    // Hold keeps the duck engaged through breaths / brief inter-word
    // gaps so the tab doesn't pump back up mid-sentence. The release
    // TAU handles the smooth ramp once we actually decide to let go.
    const shouldDuck = aboveThreshold || (now - lastVoiceActiveMs) < DUCK_HOLD_MS;
    const target = shouldDuck ? DUCK_TARGET_GAIN : PASSTHROUGH_GAIN;
    if (target === currentDuckTarget) return;

    const tau = shouldDuck ? DUCK_ATTACK_TAU : DUCK_RELEASE_TAU;
    tabDuckGain.gain.cancelScheduledValues(audioCtx.currentTime);
    tabDuckGain.gain.setTargetAtTime(target, audioCtx.currentTime, tau);
    currentDuckTarget = target;
  };
  envelopeFollowerHandle = requestAnimationFrame(tick);
}

function stopEnvelopeFollower() {
  if (envelopeFollowerHandle) {
    cancelAnimationFrame(envelopeFollowerHandle);
    envelopeFollowerHandle = null;
  }
}

// ── Captions (Speech Bubbles) ──
function addCaption(personaName, text) {
  if (!personaName) return;
  const slot = slotFor(personaName);
  if (!slot) return;
  const list = captionsByPersona.get(personaName) || [];
  list.push(text);
  while (list.length > 3) list.shift();
  captionsByPersona.set(personaName, list);
  renderCaptions(slot, list);
}

function renderCaptions(slot, list) {
  const container = slot.querySelector(".captions");
  if (!container) return;
  container.innerHTML = list
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

function spawnReaction(slot, type) {
  if (!slot) return;
  const container = slot.querySelector(".reactions");
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

// ── UI Helpers ──
function showError(msg) {
  const el = $("#setup-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function hideError() {
  $("#setup-error").classList.add("hidden");
}
