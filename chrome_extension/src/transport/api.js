/**
 * HTTP API client for the FastAPI session service. Thin layer over fetch
 * that tags errors with status so the UI can render a sanitized message
 * (raw server bodies can leak FastAPI traces or internal envelope shapes).
 */

import { API_URL } from "../config.js";

const ANONYMOUS_ID_KEY = "couchverse.anonymous_id";

// Stable per-install id, persisted in chrome.storage.local. Sent on
// every session creation so the server can later associate pre-auth
// sessions with a Clerk user (UPDATE ... WHERE anonymous_id = $1).
// Without this minted from day one, sessions captured before auth
// ships are unmergeable into any future user account.
async function getOrCreateAnonymousId() {
  const stored = await chrome.storage.local.get(ANONYMOUS_ID_KEY);
  let id = stored[ANONYMOUS_ID_KEY];
  if (!id) {
    id = crypto.randomUUID();
    await chrome.storage.local.set({ [ANONYMOUS_ID_KEY]: id });
  }
  return id;
}

export async function createSessionApi(videoUrl, videoTitle) {
  const anonymousId = await getOrCreateAnonymousId();
  const res = await fetch(`${API_URL}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      video_url: videoUrl,
      video_title: videoTitle,
      anonymous_id: anonymousId,
    }),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    const err = new Error(`Session creation failed [${res.status}]: ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// Persona manifest — server is the single source of truth for which
// personas the extension renders. Called once on panel open to populate
// the setup screen; the per-session lineup also rides on the sessions
// response (used to render the live avatar stack), so this is purely
// for the pre-session preview.
export async function fetchPersonasApi() {
  const res = await fetch(`${API_URL}/api/personas`);
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    const err = new Error(`Persona manifest failed [${res.status}]: ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// Translate raw fetch / API errors into something safe to display in the
// setup UI. The full error is still logged via console for debugging.
export function friendlyApiError(err) {
  const status = err?.status;
  if (status === 429) return "Too many requests. Please try again in a moment.";
  if (status >= 500) return "Service unavailable. Please try again shortly.";
  if (status >= 400) return "Couldn't start a session for this tab.";
  // Network / TypeError / unknown — likely offline or DNS failure.
  return "Couldn't reach the Couchverse service. Check your connection and retry.";
}
