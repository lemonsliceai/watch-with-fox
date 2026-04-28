/**
 * SessionLifecycle — orchestrates start/end of a session and routes
 * room events into the audio graph + UI. This is the only place where
 * the audio graph, room controller, tab capture, and UI all meet.
 *
 * State is owned via the `SessionState` enum (see config.js):
 *   IDLE → STARTING → LIVE → ENDING → IDLE
 *
 * Auto-end paths (avatars all gone, tab paused) check for `LIVE` so
 * they can't re-enter end during a teardown already in flight.
 */

import { Track } from "livekit-client";

import { AudioGraph } from "./audio/audio-graph.js";
import { SessionState } from "./config.js";
import { RoomController } from "./livekit/room-controller.js";
import { detectActiveMedia, syncPlayheadToAgent } from "./messaging/tab-bridge.js";
import { personaFromAvatarIdentity, resolvePersonaKey } from "./persona.js";
import { createSessionApi, friendlyApiError } from "./transport/api.js";
import { captureAndPublishTabAudio, stopTabStream } from "./transport/tab-capture.js";
import {
  addCaption,
  clearCaptions,
  hideError,
  mountAvatarVideo,
  resetAllSlots,
  setSlotSpeaking,
  showError,
  slotFor,
  spawnReaction,
} from "./ui/avatar-slots.js";
import { getPacing } from "./ui/pacing-controls.js";

const $ = (sel) => document.querySelector(sel);

export class SessionLifecycle {
  constructor() {
    this._state = SessionState.IDLE;
    this._activeTabId = null;
    this._tabAudioStream = null;

    this._room = new RoomController({
      onTrackSubscribed: this._onTrackSubscribed.bind(this),
      onTrackUnsubscribed: this._onTrackUnsubscribed.bind(this),
      onDataReceived: this._onDataReceived.bind(this),
      onActiveSpeakers: this._onActiveSpeakers.bind(this),
      onConnectionState: this._onConnectionState.bind(this),
      onDisconnected: this._onDisconnected.bind(this),
      onParticipantConnected: this._onParticipantConnected.bind(this),
      onParticipantDisconnected: this._onParticipantDisconnected.bind(this),
    });
    this._audio = new AudioGraph({ audioContainer: $("#audio-container") });

    // UI-only: which personas are currently mid-utterance. Drives slot
    // highlighting and Skip button state. Does NOT drive audio ducking
    // — that's signal-driven off the voice analysers, not these events,
    // so a late commentary_end from LemonSlice can't leave the tab
    // stuck ducked.
    this._speakingNow = new Set();
    // Personas currently mid-intro. Tracked separately so the Skip
    // button can stay disabled during intros — the intro ritual (Fox,
    // then Alien) is non-skippable, and a stray click landing between
    // the two used to cut off Alien's intro.
    this._introNow = new Set();
    // LemonSlice avatar personas currently connected. When this drains
    // to empty after at least one connected, the session auto-ends —
    // no point staying on the session screen with no comedians left.
    this._connectedAvatars = new Set();
    this._everHadAvatar = false;
  }

  get state() {
    return this._state;
  }
  get activeTabId() {
    return this._activeTabId;
  }
  setActiveTabId(id) {
    this._activeTabId = id;
  }

  // ── Public lifecycle ──

  async start() {
    if (this._state !== SessionState.IDLE) return;
    const btn = $("#start-btn");
    const videoUrl = btn.dataset.videoUrl;
    const videoTitle = btn.dataset.videoTitle || "";
    if (!videoUrl) {
      showError("No active media tab detected");
      return;
    }

    this._state = SessionState.STARTING;
    btn.disabled = true;
    btn.classList.add("loading");
    btn.textContent = "Starting...";
    hideError();

    try {
      // Build the audio graph synchronously on the user gesture so the
      // AudioContext resume is gesture-tied. The graph also has to
      // exist before the first avatar track can arrive.
      await this._audio.init();

      const session = await createSessionApi(videoUrl, videoTitle);

      $("#setup-screen").classList.add("hidden");
      $("#session-screen").classList.remove("hidden");

      await this._room.connect(session.token, session.livekit_url);
      this._tabAudioStream = await captureAndPublishTabAudio({
        tabId: this._activeTabId,
        room: this._room,
        audioGraph: this._audio,
      });

      this._state = SessionState.LIVE;
      console.log("[ext] Session started:", session.session_id);
    } catch (err) {
      console.error("[ext] Failed to start session:", err);
      // Roll back any partial setup so a retry starts from a clean
      // slate. Without this, a publishTrack failure after room.connect
      // would leak the AudioContext, the captured MediaStream, and a
      // half-wired Room with stale event handlers.
      await this._teardownPartialStart();
      showError(friendlyApiError(err));
      this._resetSetupUi();
      this._state = SessionState.IDLE;
    }
  }

  async end() {
    // LIVE is the only state where teardown work needs to happen.
    // STARTING funnels through the failure path inside `start`;
    // ENDING is a teardown already in flight; IDLE has nothing to do.
    if (this._state !== SessionState.LIVE) return;
    this._state = SessionState.ENDING;

    const endBtn = $("#end-btn");
    if (endBtn) endBtn.disabled = true;

    try {
      await this._room.dispose();
      this._audio.teardown();
      stopTabStream(this._tabAudioStream);
      this._tabAudioStream = null;
      this._resetSessionUi();
      this._resetSetupUi();
      // Re-detect media in the active tab so the start button reflects
      // current state (the page may have stopped playback while we were
      // mid-session).
      this._redetectActiveMedia();
    } finally {
      if (endBtn) endBtn.disabled = false;
      this._state = SessionState.IDLE;
    }
  }

  // Public hooks for the entry point.

  pauseFollower() {
    this._audio.stopFollower();
  }
  resumeFollower() {
    if (this._state === SessionState.LIVE) this._audio.startFollower();
  }

  skipCommentary() {
    if (this._speakingNow.size === 0) return;
    this._room.publishControl({ type: "skip" }, "podcast.control");
  }

  publishPacing() {
    const p = getPacing();
    this._room.publishControl(
      { type: "settings", frequency: p.frequency, length: p.length },
      "podcast.control",
    );
  }

  // ── Internal helpers ──

  async _teardownPartialStart() {
    try {
      await this._room.dispose();
    } catch (err) {
      console.warn("[ext] dispose during partial-start cleanup:", err);
    }
    try {
      this._audio.teardown();
    } catch (err) {
      console.warn("[ext] audio teardown during partial-start cleanup:", err);
    }
    stopTabStream(this._tabAudioStream);
    this._tabAudioStream = null;
    this._resetSessionUi();
  }

  _resetSessionUi() {
    this._speakingNow.clear();
    this._introNow.clear();
    this._connectedAvatars.clear();
    this._everHadAvatar = false;
    clearCaptions();
    resetAllSlots();
    this._updateSkipButton();
  }

  _resetSetupUi() {
    $("#session-screen").classList.add("hidden");
    $("#setup-screen").classList.remove("hidden");
    const btn = $("#start-btn");
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "Start Couchverse";
  }

  _redetectActiveMedia() {
    detectActiveMedia({
      onPreview: ({ url, title }) => {
        const btn = $("#start-btn");
        btn.disabled = false;
        btn.dataset.videoUrl = url;
        btn.dataset.videoTitle = title || "";
      },
      onNoMedia: () => {
        const btn = $("#start-btn");
        btn.disabled = true;
        delete btn.dataset.videoUrl;
        delete btn.dataset.videoTitle;
      },
    })
      .then((tabId) => {
        if (tabId != null) this._activeTabId = tabId;
      })
      .catch((err) => console.warn("[ext] re-detect after end failed:", err));
  }

  _updateSkipButton() {
    const btn = $("#skip-btn");
    if (!btn) return;
    // Disabled when nobody is mid-commentary OR when an intro is in
    // flight — intros are non-skippable so the Fox → Alien ritual
    // always plays out.
    btn.disabled = this._speakingNow.size === 0 || this._introNow.size > 0;
  }

  // ── Tab-bridge handlers (called from sidepanel.js entry) ──

  handleMediaStateUpdate(msg) {
    if (this._state !== SessionState.LIVE) return;
    if (!this._room.isConnected()) return;
    // Pausing the media cuts the tab-audio track the agent relies on
    // for STT, so there's nothing left for Fox to react to. End the
    // session rather than leave the avatar streaming into silence.
    if (!msg.playing) {
      this.end().catch((err) => console.warn("[ext] auto-end on pause failed:", err));
      return;
    }
    this._room.publishControl({ type: "play", t: msg.time }, "podcast.control");
  }

  // ── Room event handlers ──

  _onTrackSubscribed(track, publication, participant) {
    const { personaName, key } = resolvePersonaKey(participant, track, publication);

    // [DIAG] Per-track subscription log — investigating "1-in-4 silent
    // avatar". Captures every track that reaches the panel so we can tell
    // "track never arrived" apart from "arrived but graph silent".
    console.log(
      "[ext][diag] TrackSubscribed",
      "kind=", track.kind,
      "identity=", participant.identity,
      "trackName=", track.name || publication?.trackName,
      "personaName=", personaName,
      "key=", key,
    );

    if (track.kind === Track.Kind.Audio) {
      // Only attach tracks that resolve to a known persona — either by
      // LemonSlice avatar identity (`lemonslice-avatar-<name>`) or by
      // track name (`persona-<name>`). The sidechain follower takes the
      // peak RMS across every attached analyser, so any unrelated audio
      // track folded into the graph would over-duck the tab from a
      // signal that isn't actually persona voice.
      if (!personaName) {
        console.warn(
          "[ext] Ignoring unknown audio track:",
          "identity=",
          participant.identity,
          "trackName=",
          track.name || publication?.trackName,
        );
        return;
      }
      this._audio.attachPersona(track, key);
      return;
    }

    if (track.kind !== Track.Kind.Video) return;

    const isAvatarTrack =
      personaFromAvatarIdentity(participant.identity) !== null ||
      participant.attributes?.["lk.publish_on_behalf"];
    if (!isAvatarTrack) return;

    const slot = slotFor(personaName);
    if (!slot) return;
    mountAvatarVideo(slot, track);
    spawnReaction(slot, "eyes");
  }

  _onTrackUnsubscribed(track, publication, participant) {
    if (track.kind === Track.Kind.Audio) {
      const { key } = resolvePersonaKey(participant, track, publication);
      if (this._audio.hasPersona(key)) {
        this._audio.detachPersona(key);
        return;
      }
    }
    track.detach().forEach((el) => el.remove());
  }

  _onDataReceived(payload, _participant, _kind, topic) {
    let msg;
    try {
      msg = JSON.parse(new TextDecoder().decode(payload));
    } catch {
      return;
    }

    if (topic === "commentary.control" && msg.type === "agent_ready") {
      console.log("[ext] Agent ready — syncing playhead");
      this._syncPlayhead();
      this.publishPacing();
      return;
    }

    // Commentary lifecycle — drives UI state only (slot highlighting,
    // Skip button enable). Tab-audio ducking is NOT driven from here;
    // the sidechain envelope follower watches the actual persona
    // voice signal and decides for itself. That decoupling is
    // deliberate: LemonSlice's second-avatar `lk.playback_finished`
    // RPC is flaky (see server/CLAUDE.md) and a late commentary_end
    // would otherwise leave the tab stuck ducked.
    if (topic === "commentary.control" && msg.type === "commentary_start") {
      const personaName = msg.speaker;
      const phase = msg.phase || "commentary";
      if (personaName) {
        if (phase === "intro") this._introNow.add(personaName);
        else this._speakingNow.add(personaName);
        const slot = slotFor(personaName);
        slot?.classList.add("speaking");
        spawnReaction(slot, "random");
      }
      this._updateSkipButton();
      return;
    }

    if (topic === "commentary.control" && msg.type === "commentary_end") {
      const personaName = msg.speaker;
      const phase = msg.phase || "commentary";
      if (personaName) {
        if (phase === "intro") this._introNow.delete(personaName);
        else this._speakingNow.delete(personaName);
        // Only drop the "speaking" class if nobody from either set is
        // still mid-utterance for that persona.
        if (!this._speakingNow.has(personaName) && !this._introNow.has(personaName)) {
          setSlotSpeaking(personaName, false);
        }
      }
      this._updateSkipButton();
      return;
    }

    // Captions. Today the agent only publishes commentary_start /
    // commentary_end / agent_ready, so this is currently unreachable
    // — kept wired up so a future transcript-forwarding addition on
    // the agent side can land without UI changes.
    if (msg.type === "agent_transcript" || msg.text) {
      const text = msg.text || msg.content;
      if (text && msg.speaker) addCaption(msg.speaker, text);
    }
  }

  // VAD-driven active-speaker updates highlight the matching slot only
  // when commentary.control hasn't already lit it. Purely visual
  // jitter is acceptable here; commentary_start/end remains the
  // authoritative source.
  _onActiveSpeakers(speakers) {
    const localId = this._room.localParticipantIdentity;
    const activePersonas = new Set();
    for (const p of speakers) {
      if (p.identity === localId) continue;
      const personaName = personaFromAvatarIdentity(p.identity);
      if (personaName) activePersonas.add(personaName);
    }
    for (const slot of document.querySelectorAll(".avatar-slot")) {
      const name = slot.dataset.name;
      const shouldHighlight = activePersonas.has(name) || this._speakingNow.has(name);
      slot.classList.toggle("speaking", shouldHighlight);
    }
  }

  _onConnectionState(state) {
    console.log("[ext] Connection state:", state);
  }

  _onDisconnected(reason) {
    console.log("[ext] Disconnected:", reason);
  }

  _onParticipantConnected(participant) {
    const personaName = personaFromAvatarIdentity(participant.identity);
    // [DIAG] Investigating "1-in-4 silent avatar" — log every join so we
    // can confirm both LemonSlice avatars actually reach the room.
    console.log(
      "[ext][diag] ParticipantConnected",
      "identity=", participant.identity,
      "personaName=", personaName,
    );
    if (!personaName) return;
    this._connectedAvatars.add(personaName);
    this._everHadAvatar = true;
  }

  // Once every avatar that joined this session has left, there's no
  // commentary coming — auto-end so the user lands back on the start
  // screen instead of an empty stage. The `_everHadAvatar` gate
  // prevents firing during the initial connect window before any
  // avatar has shown up. The state check avoids re-entering `end`
  // when the disconnect we're handling is itself fired by an
  // explicit teardown already in flight.
  _onParticipantDisconnected(participant) {
    const personaName = personaFromAvatarIdentity(participant.identity);
    if (!personaName) return;
    this._connectedAvatars.delete(personaName);
    if (
      this._state === SessionState.LIVE &&
      this._everHadAvatar &&
      this._connectedAvatars.size === 0
    ) {
      this.end().catch((err) => console.warn("[ext] auto-end after avatars left failed:", err));
    }
  }

  _syncPlayhead() {
    syncPlayheadToAgent({
      tabId: this._activeTabId,
      onPlay: ({ t }) => this._room.publishControl({ type: "play", t }, "podcast.control"),
      onPause: () => this._room.publishControl({ type: "pause" }, "podcast.control"),
    });
  }
}
