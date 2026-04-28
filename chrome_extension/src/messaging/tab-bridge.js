/**
 * Tab bridge — everything that talks to (a) the active browser tab and
 * (b) the content script injected into it. Three concerns:
 *
 *   1. detectActiveMedia(): figure out which tab the side panel is bound
 *      to, surface its title/URL, and inject the content script if it
 *      wasn't already there.
 *   2. registerTabMessageListener(): route messages from the content
 *      script to UI handlers, dropping anything that came from a
 *      different tab (background-tab broadcasts would otherwise tear
 *      down the live session when an unrelated tab pauses).
 *   3. syncPlayheadToAgent(): sync the agent to the current playhead
 *      after `agent_ready`.
 */

export function isCapturableTabUrl(url) {
  if (!url) return false;
  // chrome://, edge://, about:, file:, view-source:, etc. can't be tab-captured.
  return url.startsWith("http://") || url.startsWith("https://");
}

// Trim the trailing " - Site Name" / " | Site Name" / " — Site Name"
// that most sites tack onto <title>. Leaves the leading content (which
// is almost always the actual media title) untouched.
export function stripTitleSuffix(title) {
  return title.replace(/\s+[-|–—]\s+[^-|–—]+$/, "").trim();
}

// Resolve the active tab and surface its info to the UI. Detection
// never blocks on the content script — if the page was open before the
// extension was (re)loaded, the content script was never injected and
// `chrome.tabs.sendMessage` would fail silently, leaving the UI stuck
// on "Detecting video...". Instead, derive what we can directly from
// the tab's metadata, which is always available.
//
// The content script is still useful for runtime events (play/pause/
// seek monitoring during a session), so if it isn't responding we
// inject it programmatically via chrome.scripting.
//
// Returns the tab.id of the active tab (null if we can't bind), so the
// caller can store it and use it as the filter for cross-tab messages.
export async function detectActiveMedia({ onPreview, onNoMedia }) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab) return null;

  if (!isCapturableTabUrl(tab.url)) {
    onNoMedia();
    return tab.id ?? null;
  }

  // Use the tab's own metadata as the immediate preview.
  const title = stripTitleSuffix(tab.title || "") || tab.url;
  onPreview({ url: tab.url, title });

  // Ping the content script for richer info (and to confirm it's alive).
  // If it doesn't reply, inject it so play/pause monitoring works once
  // the session starts.
  try {
    const info = await chrome.tabs.sendMessage(tab.id, { type: "get-video-info" });
    if (info) onPreview(info);
  } catch {
    console.log("[ext] Content script not present, injecting...");
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
      // Freshly-injected script will push a media-video-info shortly.
    } catch (err) {
      console.warn("[ext] Content script injection failed:", err);
    }
  }
  return tab.id;
}

// Register a runtime.onMessage listener that routes to the supplied
// handlers — but only for messages from the tab this side panel is
// bound to. The content script is injected into every http(s) page,
// so background tabs broadcast media state too; without the filter a
// paused YouTube tab in the background tears down the live session.
//
// `getActiveTabId` is read on every dispatch (not snapshotted) so the
// caller can swap which tab is active without re-registering.
export function registerTabMessageListener(getActiveTabId, handlers) {
  chrome.runtime.onMessage.addListener((msg, sender) => {
    const activeTabId = getActiveTabId();
    if (sender?.tab?.id != null && sender.tab.id !== activeTabId) return;
    const handler = handlers[msg?.type];
    if (handler) handler(msg);
  });
}

// Ask the content script for the current playhead, then push a
// matching `play` / `pause` to the agent. Called after `agent_ready`
// so a freshly-connected agent picks up wherever the user actually is
// rather than treating the session as starting at t=0.
export async function syncPlayheadToAgent({ tabId, onPlay, onPause }) {
  if (!tabId) return;
  try {
    const state = await chrome.tabs.sendMessage(tabId, {
      type: "get-video-state",
    });
    if (!state) return;

    const SYNC_FORWARD_SEC = 0.7;
    if (state.playing) {
      await onPlay({ t: Math.max(0, state.time + SYNC_FORWARD_SEC) });
    } else {
      await onPause();
    }
  } catch (err) {
    console.warn("[ext] Failed to sync playhead:", err);
  }
}
