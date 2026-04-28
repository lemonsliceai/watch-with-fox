/**
 * Pacing controls — two segmented controls (chattiness, reply length)
 * persisted to localStorage. Module owns the segmented-button widget
 * behavior, the persistence schema, and the in-memory snapshot.
 *
 * The orchestration layer subscribes via `onChange` and is responsible
 * for publishing to the agent — this module doesn't know about LiveKit.
 */

import { PACING_DEFAULTS, PACING_SCHEMA_VERSION, PACING_STORAGE_KEY } from "../config.js";

const pacing = { ...PACING_DEFAULTS };
let onChangeCallback = null;

export function initPacingControls(onChange) {
  onChangeCallback = onChange ?? null;
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

// Read-only snapshot for callers that need to push current settings
// (e.g. on agent_ready).
export function getPacing() {
  return { ...pacing };
}

function selectPacing(setting, value) {
  if (!value || pacing[setting] === value) return;
  pacing[setting] = value;
  savePacing();
  const group = document.querySelector(`.segmented[data-setting="${setting}"]`);
  if (group) syncSegmentedGroup(group, value);
  onChangeCallback?.(getPacing());
}

function syncSegmentedGroup(group, activeValue) {
  for (const btn of group.querySelectorAll(".seg-btn")) {
    btn.classList.toggle("is-active", btn.dataset.value === activeValue);
  }
}

function loadPacing() {
  try {
    const raw = localStorage.getItem(PACING_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed?.version !== PACING_SCHEMA_VERSION) return {};
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
    localStorage.setItem(
      PACING_STORAGE_KEY,
      JSON.stringify({ version: PACING_SCHEMA_VERSION, ...pacing }),
    );
  } catch {
    // Private mode / quota — silently ignore; the UI still works per-session.
  }
}
