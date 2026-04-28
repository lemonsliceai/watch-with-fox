/**
 * Audio graph — owns the shared AudioContext, tab-audio loopback gain,
 * per-persona voice nodes, and the sidechain envelope follower that
 * drives ducking off the actual voice signal.
 *
 *   tab audio          → tabDuckGain ──────────────────────────────┐
 *                                                                  ▼
 *   persona voice #1   → trimGain → ┬→ destination              destination
 *                                   └→ analyser (sidechain tap) ┘
 *   persona voice #2   → trimGain → ┬→ destination
 *                                   └→ analyser (sidechain tap)
 *
 * One AudioContext is required because the follower (on the voice side)
 * needs to influence the tab gain — they have to share a context.
 *
 * Why sidechain off the voice signal rather than off server start/end
 * events? Three properties this gets us for free:
 *   * Any persona's voice ducks the tab at the same depth — no per-
 *     speaker tuning needed, and adding a third comedian is zero-config.
 *   * A persona's own voice CANNOT duck itself — avatar audio is on a
 *     different branch from tabDuckGain, by graph construction.
 *   * Late or missing commentary_end events from LemonSlice can't leave
 *     the tab stuck ducked — when the voice stops producing samples,
 *     RMS drops below threshold and the gain releases.
 *   * Per-persona trim normalizes ElevenLabs voice-loudness differences
 *     (Fanz is softer than Dave) without touching the duck logic.
 *
 * The Web Audio API doesn't expose a native sidechain input
 * (webaudio/web-audio-api#246), so we roll our own envelope follower.
 */

import {
  DEFAULT_PERSONA_TRIM,
  DUCK_ATTACK_TAU,
  DUCK_HOLD_MS,
  DUCK_RELEASE_TAU,
  DUCK_RMS_THRESHOLD,
  DUCK_TARGET_GAIN,
  PASSTHROUGH_GAIN,
  PERSONA_TRIM_GAIN,
} from "../config.js";

export class AudioGraph {
  constructor({ audioContainer }) {
    // The hidden DOM element where each persona's <audio> sink lives.
    // It exists purely as a WebRTC consumer (muted, kept in DOM) so
    // Chrome keeps pulling RTP samples through the remote track; the
    // audible path is the WebAudio graph fed by createMediaStreamSource
    // on the underlying MediaStreamTrack. See attachPersona below for
    // why we don't use createMediaElementSource here.
    this._audioContainer = audioContainer;
    this._ctx = null;
    this._tabDuckGain = null;
    // personaName → { source, trimGain, analyser, buffer, audioEl, track }
    this._personaNodes = new Map();
    // Envelope follower state.
    this._followerHandle = null;
    this._lastVoiceActiveMs = 0;
    this._currentDuckTarget = PASSTHROUGH_GAIN;
  }

  // Resume must be *initiated* on the user gesture. Modern Chrome lets
  // the gesture chain extend across awaits, so awaiting here is safe and
  // guarantees the context is `running` before downstream code creates
  // MediaStreamSources against it.
  async init() {
    if (this._ctx) return;
    this._ctx = new AudioContext();
    if (this._ctx.state === "suspended") {
      try {
        await this._ctx.resume();
      } catch (err) {
        console.warn("[ext] audioCtx.resume failed:", err);
      }
    }
    this._tabDuckGain = this._ctx.createGain();
    this._tabDuckGain.gain.value = PASSTHROUGH_GAIN;
    this._tabDuckGain.connect(this._ctx.destination);

    this._lastVoiceActiveMs = 0;
    this._currentDuckTarget = PASSTHROUGH_GAIN;
    this.startFollower();
  }

  teardown() {
    this.stopFollower();
    for (const key of Array.from(this._personaNodes.keys())) {
      this.detachPersona(key);
    }
    if (this._ctx) {
      try {
        this._ctx.close();
      } catch {}
      this._ctx = null;
      this._tabDuckGain = null;
    }
  }

  // Route the captured tab audio through tabDuckGain so the loopback
  // path goes through the same gain the follower modulates. Returns
  // `true` on success — caller can publish the raw stream regardless,
  // but the user won't hear the tab if this returns false.
  attachTabStream(stream) {
    if (!this._ctx || !this._tabDuckGain) return false;
    const source = this._ctx.createMediaStreamSource(stream);
    source.connect(this._tabDuckGain);
    return true;
  }

  // Route a persona's voice track through (source → trim → destination)
  // with an analyser tap for the sidechain. Idempotent on key — calling
  // twice replaces the previous node so late reconnects don't leak
  // disconnected graph nodes.
  attachPersona(track, key) {
    if (this._personaNodes.has(key)) this.detachPersona(key);

    // [DIAG] Track-attach diagnostics — investigating "1-in-4 silent avatar"
    // symptom. Logs the underlying MediaStreamTrack state at attach time so
    // we can tell apart "track never delivered" vs "delivered but silent".
    const mst = track.mediaStreamTrack;
    console.log(
      "[ext][diag] attachPersona begin",
      "key=", key,
      "ctx.state=", this._ctx?.state,
      "mst.id=", mst?.id,
      "mst.enabled=", mst?.enabled,
      "mst.muted=", mst?.muted,
      "mst.readyState=", mst?.readyState,
    );

    // Keep a muted <audio> element in the DOM purely as a WebRTC receiver
    // wake-up: Chrome won't pull RTP samples through a remote track
    // unless something is consuming it, and a playing media element is
    // the cheapest way to keep the pipe open. The graph below drives
    // the speakers via createMediaStreamSource on the underlying
    // MediaStreamTrack — the element itself emits no sound.
    //
    // We previously used createMediaElementSource here, which is
    // *supposed* to divert the element's audio into the graph. In a
    // multi-avatar room the second avatar's source node intermittently
    // failed to divert cleanly: the element kept playing at its default
    // 1.0 gain through the default output, while the graph never saw
    // samples. When that bit Alien, the trim never applied AND the
    // analyser never fired the duck — Alien sounded quiet AND the tab
    // stayed loud, which read as Alien "ducking themself". Pulling
    // from the MediaStreamTrack directly avoids the divert path
    // entirely and keeps the graph the single source of truth for what
    // reaches the speakers.
    //
    // `muted = true` (not `volume = 0`) is load-bearing: muted leaves
    // the element actively consuming RTP, while volume=0 can trip
    // Chrome's inaudible-track optimization and stop the pull.
    const el = track.attach();
    el.muted = true;
    this._audioContainer.appendChild(el);

    // Fallback shape: unmute the element so the user still hears the
    // comedian (no trim, no sidechain tap), and detachPersona can clean
    // it up the same way. Better than silence.
    const registerFallback = (reason) => {
      console.warn("[ext][diag] attachPersona fallback path", "key=", key, "reason=", reason);
      el.muted = false;
      this._personaNodes.set(key, {
        source: null,
        trimGain: null,
        analyser: null,
        buffer: null,
        audioEl: el,
        track,
      });
    };

    if (!this._ctx) {
      registerFallback("no AudioContext");
      return;
    }

    let source;
    try {
      // Wrap the underlying MediaStreamTrack in a fresh MediaStream so
      // the source node has a stable handle independent of any stream
      // LiveKit's attach() set on the element.
      const mediaStream = new MediaStream([track.mediaStreamTrack]);
      source = this._ctx.createMediaStreamSource(mediaStream);
    } catch (err) {
      console.warn("[ext] createMediaStreamSource failed for", key, err);
      registerFallback("createMediaStreamSource threw");
      return;
    }

    const trimGain = this._ctx.createGain();
    trimGain.gain.value = PERSONA_TRIM_GAIN[key] ?? DEFAULT_PERSONA_TRIM;

    const analyser = this._ctx.createAnalyser();
    // 1024 samples ≈ 21ms at 48kHz — a whole syllable fits, so RMS
    // reads smoothly without needing heavy smoothing constants.
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.1;
    const buffer = new Float32Array(analyser.fftSize);

    source.connect(trimGain);
    trimGain.connect(this._ctx.destination);
    // Sidechain tap — post-trim so the follower sees user-perceived
    // loudness (same level the listener hears). Post-trim also means a
    // mis-set trim can't leave the duck silently mistuned.
    trimGain.connect(analyser);

    this._personaNodes.set(key, {
      source,
      trimGain,
      analyser,
      buffer,
      audioEl: el,
      track,
      // [DIAG] running max RMS observed by the follower since attach.
      // Updated in the follower tick; read by the silence sentinel below.
      peakRmsObserved: 0,
      attachedAt: performance.now(),
    });

    // [DIAG] Silence sentinel — investigating "1-in-4 silent avatar". The
    // follower's rAF tick keeps `peakRmsObserved` updated; this fires once
    // 12s after attach and reports whether ANY non-silent samples arrived
    // across the entire window. 12s comfortably covers Fox's intro AND
    // Alien's intro (Alien speaks ~5-7s post-attach, finishing ~10-12s),
    // so a still-zero peak means the graph is plumbed but no samples ever
    // flowed — the exact "second source node failed to divert" symptom
    // that motivated the createMediaElementSource → createMediaStreamSource
    // switch. (We only schedule one shot per attach; detachPersona makes
    // the late callback a no-op via the personaNodes lookup.)
    setTimeout(() => {
      const node = this._personaNodes.get(key);
      if (!node || !node.analyser) return;
      const mstNow = node.track?.mediaStreamTrack;
      const peak = node.peakRmsObserved;
      const log = peak < 1e-6 ? console.warn : console.log;
      log(
        "[ext][diag] silence sentinel",
        "key=", key,
        "peakRmsObserved=", peak.toExponential(2),
        "mst.enabled=", mstNow?.enabled,
        "mst.muted=", mstNow?.muted,
        "mst.readyState=", mstNow?.readyState,
        peak < 1e-6 ? "(SILENT — graph plumbed but no audio frames in 12s)" : "",
      );
    }, 12000);
  }

  detachPersona(key) {
    const node = this._personaNodes.get(key);
    if (!node) return;
    try {
      node.source?.disconnect();
    } catch {}
    try {
      node.trimGain?.disconnect();
    } catch {}
    try {
      node.analyser?.disconnect();
    } catch {}
    try {
      node.track?.detach(node.audioEl);
    } catch {}
    node.audioEl?.remove();
    this._personaNodes.delete(key);
  }

  hasPersona(key) {
    return this._personaNodes.has(key);
  }

  // ── Sidechain envelope follower ──
  //
  // Runs per animation frame. Reads peak RMS across all persona
  // analysers (max, not sum — two quiet voices shouldn't duck deeper
  // than one loud voice), compares to threshold + hold window, drives
  // tabDuckGain.
  //
  // rAF is the right scheduler: when the side panel is hidden we stop
  // the follower entirely (lifecycle handler in sidepanel.js) so
  // background-tab throttling never kicks in. Upgrading to an
  // AudioWorklet would buy us frame-accurate response in exchange for
  // a separate worklet module — not worth it yet.
  startFollower() {
    if (this._followerHandle) return;
    const tick = () => {
      this._followerHandle = requestAnimationFrame(tick);
      if (!this._ctx || !this._tabDuckGain) return;

      let peakRms = 0;
      for (const node of this._personaNodes.values()) {
        if (!node.analyser) continue;
        node.analyser.getFloatTimeDomainData(node.buffer);
        let sumSquares = 0;
        for (let i = 0; i < node.buffer.length; i++) {
          const s = node.buffer[i];
          sumSquares += s * s;
        }
        const rms = Math.sqrt(sumSquares / node.buffer.length);
        if (rms > peakRms) peakRms = rms;
        // [DIAG] Per-persona peak — read by the silence sentinel.
        if (rms > node.peakRmsObserved) node.peakRmsObserved = rms;
      }

      const now = performance.now();
      const aboveThreshold = peakRms > DUCK_RMS_THRESHOLD;
      if (aboveThreshold) this._lastVoiceActiveMs = now;
      // Hold keeps the duck engaged through breaths / brief inter-word
      // gaps so the tab doesn't pump back up mid-sentence. The release
      // TAU handles the smooth ramp once we actually decide to let go.
      const shouldDuck = aboveThreshold || now - this._lastVoiceActiveMs < DUCK_HOLD_MS;
      const target = shouldDuck ? DUCK_TARGET_GAIN : PASSTHROUGH_GAIN;
      if (target === this._currentDuckTarget) return;

      const tau = shouldDuck ? DUCK_ATTACK_TAU : DUCK_RELEASE_TAU;
      this._tabDuckGain.gain.cancelScheduledValues(this._ctx.currentTime);
      this._tabDuckGain.gain.setTargetAtTime(target, this._ctx.currentTime, tau);
      this._currentDuckTarget = target;
    };
    this._followerHandle = requestAnimationFrame(tick);
  }

  stopFollower() {
    if (this._followerHandle) {
      cancelAnimationFrame(this._followerHandle);
      this._followerHandle = null;
    }
  }
}
