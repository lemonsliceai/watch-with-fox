/**
 * Centralized constants. Anything tuned for the product (gain depths,
 * threshold levels, schema versions, prefix conventions agreed with the
 * agent) lives here so the modules that consume them don't ship private
 * forks of the same magic numbers.
 */

// Inlined at build time from `API_URL` in chrome_extension/.env (see
// build.js + .env.example). Defaults to the hosted Couchverse API;
// override to http://localhost:8080 for local backend development.
export const API_URL = __API_URL__;

// Naming conventions agreed with the agent. LemonSlice avatar
// participants are named lemonslice-avatar-<persona>; audio-only personas
// publish each TTS track named persona-<name> from the agent's single
// local participant. Both prefixes are stripped to recover the persona
// name used as a routing key throughout the UI.
export const AVATAR_IDENTITY_PREFIX = "lemonslice-avatar-";
export const PERSONA_TRACK_PREFIX = "persona-";

// Audio graph tunables (all consumed by AudioGraph).
export const PASSTHROUGH_GAIN = 1.0;
// RMS threshold above which a persona is considered "actively speaking".
// ~0.01 ≈ -40 dB — comfortably above TTS-idle noise floor and well below
// any real speech energy.
export const DUCK_RMS_THRESHOLD = 0.01;
// Depth and time constants for the sidechain duck. Attack is short so
// the tab drops before the first syllable is stepped on; release is slow
// enough to ride out breaths without pumping. Hold keeps the duck
// engaged for a beat after the signal drops below threshold.
export const DUCK_TARGET_GAIN = 0.15; // ~-16 dB
export const DUCK_ATTACK_TAU = 0.05; // seconds
export const DUCK_RELEASE_TAU = 0.3; // seconds
export const DUCK_HOLD_MS = 500;
// Per-persona output trim. ElevenLabs voices ship at different reference
// loudness; this normalizes them at the client so the mix is balanced.
// Add new personas here as they're introduced.
export const PERSONA_TRIM_GAIN = {
  fox: 1.0, // Dave voice — our reference level
  chaos_agent: 1.6, // Fanz ships noticeably softer than Dave
};
export const DEFAULT_PERSONA_TRIM = 1.0;

// Pacing persistence. Bump PACING_SCHEMA_VERSION whenever the on-disk
// shape changes — `loadPacing` rejects mismatched versions so users with
// old data get fresh defaults instead of a half-migrated mix.
export const PACING_STORAGE_KEY = "couchverse.pacing";
export const PACING_SCHEMA_VERSION = 1;
export const PACING_DEFAULTS = { frequency: "normal", length: "normal" };

// Session lifecycle states. Single source of truth for what the session
// can and cannot do right now:
//
//   idle      — no room, no audio graph; only `start` is valid
//   starting  — `start` mid-await; auto-end paths must wait
//   live      — room connected; auto-end paths can fire
//   ending    — `end` mid-await; further end requests are no-ops
//
// Replaces the older single `sessionBusy` boolean — that flag conflated
// "starting" and "ending" so a `ParticipantDisconnected` arriving during
// teardown could re-enter `end`, hit the same flag, and silently drop
// the work needed to rebuild from a partially-torn-down state.
export const SessionState = Object.freeze({
  IDLE: "idle",
  STARTING: "starting",
  LIVE: "live",
  ENDING: "ending",
});
