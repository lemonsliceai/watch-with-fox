/**
 * Persona-name resolution. Two pieces of identity reach the client:
 *
 *   - The participant identity (carries the avatar persona for LemonSlice
 *     participants — `lemonslice-avatar-<name>`)
 *   - The track name (carries the persona for audio-only direct-publish
 *     personas — `persona-<name>`, since they all share the agent's
 *     single local participant identity)
 *
 * `resolvePersonaKey` is the single source of truth for "which persona
 * owns this track?" — used by both subscribe and unsubscribe paths so
 * the routing key never disagrees between them.
 */

import { AVATAR_IDENTITY_PREFIX, PERSONA_TRACK_PREFIX } from "./config.js";

export function personaFromAvatarIdentity(identity) {
  if (!identity || !identity.startsWith(AVATAR_IDENTITY_PREFIX)) return null;
  return identity.slice(AVATAR_IDENTITY_PREFIX.length);
}

export function personaFromTrackName(name) {
  if (!name || !name.startsWith(PERSONA_TRACK_PREFIX)) return null;
  return name.slice(PERSONA_TRACK_PREFIX.length);
}

// Returns { personaName, key }. `personaName` is null when neither prefix
// matches — falls back to a synthetic `id:<identity>` key so personaNodes
// stays unique per remote participant for unexpected debug tracks.
export function resolvePersonaKey(participant, track, publication) {
  const personaName =
    personaFromAvatarIdentity(participant.identity) ||
    personaFromTrackName(track?.name || publication?.trackName);
  return {
    personaName,
    key: personaName || `id:${participant.identity}`,
  };
}
