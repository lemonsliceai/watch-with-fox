/**
 * Background service worker — tab audio capture and side panel management.
 *
 * Responsibilities:
 *   1. Provide tab capture stream IDs to the side panel
 *   2. Enable the side panel on any http(s) page
 *   3. Relay messages between content script and side panel
 */

// Per-tab caches of the most recent media info / state from each content
// script, so the side panel can poll without round-tripping into the page.
// Cleared when the tab is closed (see `tabs.onRemoved` below).
const latestVideoInfo = {};
const latestVideoState = {};

// Enable side panel on any http(s) page. Tab audio capture works on any tab
// the user can grant access to via the action click, so we don't restrict to
// a specific site.
function isCapturablePage(url) {
  if (!url) return false;
  return url.startsWith("http://") || url.startsWith("https://");
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // `onUpdated` fires multiple times per navigation (status: "loading",
  // then "complete", plus title/favicon changes). Only react when the
  // URL actually changes — `setOptions` is idempotent but the calls add
  // up to a lot of noise during heavy browsing.
  if (!changeInfo.url || !tab.url) return;
  const enabled = isCapturablePage(tab.url);
  chrome.sidePanel.setOptions({
    tabId,
    enabled,
    path: enabled ? "sidepanel.html" : undefined,
  });
});

// Open side panel when extension icon is clicked
chrome.action.onClicked.addListener((tab) => {
  chrome.sidePanel.open({ tabId: tab.id });
});

// Drop per-tab cache entries when the tab closes. Without this every tab
// the user has ever opened during the session-worker's lifetime stays
// referenced — MV3 idles the worker after ~30s, so it self-heals in
// practice, but a long-running side panel keeps it alive indefinitely.
chrome.tabs.onRemoved.addListener((tabId) => {
  delete latestVideoInfo[tabId];
  delete latestVideoState[tabId];
});

// Handle messages from side panel and content scripts
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "capture-tab-audio") {
    handleTabCapture(msg.tabId, sendResponse);
    return true; // async response
  }

  if (msg.type === "get-active-tab") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      sendResponse(tabs[0] || null);
    });
    return true;
  }

  // Cache the most recent media info / state from each content script so the
  // side panel can poll without round-tripping into the page.
  if (msg.type === "media-video-info") {
    latestVideoInfo[sender.tab?.id] = msg;
  }
  if (msg.type === "media-state-update") {
    latestVideoState[sender.tab?.id] = msg;
  }

  if (msg.type === "get-video-info") {
    const info = latestVideoInfo[msg.tabId] || null;
    sendResponse(info);
    return false;
  }

  if (msg.type === "get-video-state") {
    const state = latestVideoState[msg.tabId] || null;
    sendResponse(state);
    return false;
  }
});

/**
 * Capture tab audio and return a stream ID to the caller.
 *
 * The stream ID is passed to the side panel which calls getUserMedia()
 * with chromeMediaSource: "tab" to get a MediaStream of the tab's audio.
 */
function handleTabCapture(tabId, sendResponse) {
  const targetTabId = tabId || undefined;

  if (targetTabId) {
    captureTab(targetTabId, sendResponse);
  } else {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs[0]) {
        sendResponse({ error: "No active tab" });
        return;
      }
      captureTab(tabs[0].id, sendResponse);
    });
  }
}

function captureTab(tabId, sendResponse) {
  chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
    if (chrome.runtime.lastError) {
      console.error("[bg] Tab capture error:", chrome.runtime.lastError.message);
      sendResponse({ error: chrome.runtime.lastError.message });
      return;
    }
    console.log("[bg] Tab capture stream ID obtained for tab", tabId);
    sendResponse({ streamId });
  });
}
