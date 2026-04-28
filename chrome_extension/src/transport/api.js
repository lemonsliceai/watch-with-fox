/**
 * HTTP API client for the FastAPI session service. Thin layer over fetch
 * that tags errors with status so the UI can render a sanitized message
 * (raw server bodies can leak FastAPI traces or internal envelope shapes).
 */

import { API_URL } from "../config.js";

export async function createSessionApi(videoUrl, videoTitle) {
  const res = await fetch(`${API_URL}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      video_url: videoUrl,
      video_title: videoTitle,
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
