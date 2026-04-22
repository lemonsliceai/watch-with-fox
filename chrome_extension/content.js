/**
 * Content script — injected into YouTube watch pages.
 *
 * Monitors the HTML5 <video> element for play/pause/seek events and
 * reports them to the extension runtime (background + side panel).
 * Also extracts video URL and title for session creation.
 */

(function () {
  "use strict";

  let video = null;
  let observer = null;
  let lastReportedTime = -1;

  function init() {
    video = document.querySelector("video");
    if (!video) {
      // YouTube SPA — video element may not exist yet. Observe DOM.
      observer = new MutationObserver(() => {
        video = document.querySelector("video");
        if (video) {
          observer.disconnect();
          observer = null;
          attachListeners();
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
      return;
    }
    attachListeners();
  }

  function attachListeners() {
    if (!video) return;

    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeked", onSeeked);

    // Report initial state
    sendVideoInfo();
    sendStateUpdate();

    // YouTube is an SPA — detect navigation to new videos
    let lastUrl = location.href;
    const urlObserver = new MutationObserver(() => {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        // Re-find video element (may change on navigation)
        setTimeout(() => {
          const newVideo = document.querySelector("video");
          if (newVideo && newVideo !== video) {
            video.removeEventListener("play", onPlay);
            video.removeEventListener("pause", onPause);
            video.removeEventListener("seeked", onSeeked);
            video = newVideo;
            video.addEventListener("play", onPlay);
            video.addEventListener("pause", onPause);
            video.addEventListener("seeked", onSeeked);
          }
          sendVideoInfo();
          sendStateUpdate();
        }, 1000);
      }
    });
    urlObserver.observe(document.body, { childList: true, subtree: true });
  }

  function onPlay() {
    sendStateUpdate();
  }

  function onPause() {
    sendStateUpdate();
  }

  function onSeeked() {
    sendStateUpdate();
  }

  function sendVideoInfo() {
    const info = {
      type: "yt-video-info",
      url: location.href,
      title: getVideoTitle(),
      videoId: getVideoId(),
    };
    chrome.runtime.sendMessage(info).catch(() => {});
  }

  function sendStateUpdate() {
    if (!video) return;
    const time = video.currentTime;
    const playing = !video.paused;
    const msg = {
      type: "yt-state-update",
      playing,
      time,
      duration: video.duration || 0,
    };
    chrome.runtime.sendMessage(msg).catch(() => {});
    lastReportedTime = time;
  }

  function getVideoTitle() {
    // YouTube renders the title in an h1 inside #above-the-fold
    const h1 = document.querySelector(
      "#above-the-fold h1.ytd-watch-metadata yt-formatted-string"
    );
    if (h1) return h1.textContent.trim();
    // Fallback
    return document.title.replace(" - YouTube", "").trim();
  }

  function getVideoId() {
    const params = new URLSearchParams(location.search);
    return params.get("v") || "";
  }

  // Listen for messages from side panel (via background or direct)
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "get-video-info") {
      sendResponse({
        type: "yt-video-info",
        url: location.href,
        title: getVideoTitle(),
        videoId: getVideoId(),
      });
      return false;
    }

    if (msg.type === "get-video-state") {
      if (!video) {
        sendResponse({ type: "yt-state-update", playing: false, time: 0, duration: 0 });
      } else {
        sendResponse({
          type: "yt-state-update",
          playing: !video.paused,
          time: video.currentTime,
          duration: video.duration || 0,
        });
      }
      return false;
    }
  });

  // Run on load
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
