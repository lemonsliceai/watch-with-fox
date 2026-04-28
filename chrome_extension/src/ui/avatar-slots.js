/**
 * Avatar slot DOM — everything that touches `.avatar-slot` elements.
 *
 * Each slot represents one persona's panel: still preview, live video
 * mount point, badge, captions stack, reactions overlay. The HTML
 * declares the slots up front (one per persona); this module fills
 * them with runtime content.
 */

const $ = (sel) => document.querySelector(sel);

export function slotFor(personaName) {
  if (!personaName) return null;
  return document.querySelector(`.avatar-slot[data-name="${personaName}"]`);
}

// Mount a freshly-attached LiveKit video element inside the slot's
// .avatar-video container. Sizing/object-fit live in CSS — this stays
// purely behavioral.
export function mountAvatarVideo(slot, track) {
  const container = slot.querySelector(".avatar-video");
  if (!container) return;
  const el = track.attach();
  container.replaceChildren(el);
  // Swap the still preview for the live video. The `video-live` class
  // drives a fade-in on the video + fade-out on the still image so the
  // transition reads as the preview "animating into" the avatar.
  slot.classList.add("video-live", "breathing");
}

// Reset every slot to its preview-only state for the next session.
export function resetAllSlots() {
  for (const slot of document.querySelectorAll(".avatar-slot")) {
    slot.classList.remove("speaking", "breathing", "video-live");
    const videoContainer = slot.querySelector(".avatar-video");
    if (videoContainer) videoContainer.replaceChildren();
  }
  for (const captions of document.querySelectorAll(".avatar-slot .captions")) {
    captions.replaceChildren();
  }
}

// Add or remove the "speaking" class for a given persona. Used by both
// commentary_start/end (authoritative) and active-speaker VAD updates
// (visual-only fallback).
export function setSlotSpeaking(personaName, isSpeaking) {
  const slot = slotFor(personaName);
  if (!slot) return;
  slot.classList.toggle("speaking", isSpeaking);
}

// ── Captions ──
//
// Per-persona caption history kept here so the orchestration layer
// doesn't need to thread a Map through every render call. The agent
// currently doesn't publish transcripts (only commentary_start/end
// + agent_ready), so this path is dormant — kept wired up so a future
// transcript-forwarding addition can land without UI changes.

const captionsByPersona = new Map();
const MAX_CAPTIONS = 3;

export function addCaption(personaName, text) {
  if (!personaName || !text) return;
  const slot = slotFor(personaName);
  if (!slot) return;
  const list = captionsByPersona.get(personaName) || [];
  list.push(text);
  while (list.length > MAX_CAPTIONS) list.shift();
  captionsByPersona.set(personaName, list);
  renderCaptions(slot, list);
}

export function clearCaptions() {
  captionsByPersona.clear();
}

function renderCaptions(slot, list) {
  const container = slot.querySelector(".captions");
  if (!container) return;
  // Build via DOM so caption text (which originates from agent LLM
  // output) can never be interpreted as HTML — `textContent` short-
  // circuits the parser entirely. Avoid `innerHTML` anywhere in this
  // path even if the agent is trusted: defense in depth.
  const bubbles = list.map((text) => {
    const bubble = document.createElement("div");
    bubble.className = "speech-bubble";
    bubble.textContent = text;
    return bubble;
  });
  container.replaceChildren(...bubbles);
}

// ── Floating reactions ──

const REACTION_SETS = {
  laugh: ["\u{1F602}", "\u{1F923}", "\u{1F606}", "\u{1F60F}"],
  love: ["\u{2764}\u{FE0F}", "\u{1F9E1}", "\u{1F525}"],
  eyes: ["\u{1F440}", "\u{2728}", "\u{1F98A}"],
  fire: ["\u{1F525}", "\u{1F4A5}", "\u{26A1}"],
};
const REACTION_LIFETIME_MS = 2000;

export function spawnReaction(slot, type) {
  if (!slot) return;
  const container = slot.querySelector(".reactions");
  if (!container) return;

  const sets = Object.keys(REACTION_SETS);
  const key = type === "random" ? sets[Math.floor(Math.random() * sets.length)] : type;
  const emojis = REACTION_SETS[key] || REACTION_SETS.laugh;
  const emoji = emojis[Math.floor(Math.random() * emojis.length)];

  const particle = document.createElement("span");
  particle.className = "reaction-particle";
  particle.textContent = emoji;
  // Random horizontal drift gives the cluster shape rather than a vertical column.
  const drift = (Math.random() - 0.5) * 40;
  particle.style.setProperty("--drift", `${drift}px`);
  particle.style.animationDelay = `${Math.random() * 0.2}s`;

  container.appendChild(particle);
  setTimeout(() => particle.remove(), REACTION_LIFETIME_MS);
}

// ── Setup-screen status ──

export function showError(msg) {
  const el = $("#setup-error");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
}

export function hideError() {
  $("#setup-error")?.classList.add("hidden");
}
