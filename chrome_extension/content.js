/**
 * Content script — injected into any http(s) page.
 *
 * Watches for the page's primary HTMLMediaElement (<video> or <audio>) and
 * reports play/pause/seek + URL/title changes to the extension runtime so
 * the side panel can sync session state and end the session when playback
 * stops. Designed to work on any streaming site, not just YouTube.
 */

(() => {
  // Guard against double-injection. The side panel programmatically
  // injects this script via chrome.scripting when it can't find a content
  // script already running (e.g. tabs opened before the extension loaded).
  // If the manifest match also fired, we'd start two parallel observers
  // and double-fire every state-update message. The window flag is the
  // canonical idempotency primitive for content scripts.
  if (window.__couchverseContentInjected) return;
  window.__couchverseContentInjected = true;

  const MEDIA_EVENTS = ["play", "pause", "seeked", "ended"];
  let media = null;
  let bodyObserver = null;
  let urlObserver = null;
  let lastUrl = location.href;

  function bindMediaEvents(el) {
    for (const evt of MEDIA_EVENTS) el.addEventListener(evt, sendStateUpdate);
  }
  function unbindMediaEvents(el) {
    for (const evt of MEDIA_EVENTS) el.removeEventListener(evt, sendStateUpdate);
  }

  function init() {
    findMedia();
    if (!media) {
      // Page may load its player asynchronously (SPAs, lazy-mounted iframes,
      // etc.). Watch the DOM until a media element appears.
      bodyObserver = new MutationObserver(() => {
        if (media && document.contains(media)) return;
        findMedia();
        if (media) {
          bodyObserver.disconnect();
          bodyObserver = null;
          attachListeners();
        }
      });
      bodyObserver.observe(document.body, { childList: true, subtree: true });
      // Even without a media element, surface the page metadata so the side
      // panel can still allow tab-audio capture (some sites use Web Audio
      // without an HTMLMediaElement).
      sendVideoInfo();
      startUrlObserver();
      return;
    }
    attachListeners();
  }

  function findMedia() {
    // Prefer the largest visible <video>; fall back to any <video>, then to
    // <audio>. Many sites have hidden preview/ad videos in the DOM, so
    // picking the largest avoids latching onto the wrong element.
    const videos = Array.from(document.querySelectorAll("video"));
    let best = null;
    let bestArea = 0;
    for (const v of videos) {
      const rect = v.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (area > bestArea) {
        best = v;
        bestArea = area;
      }
    }
    media = best || videos[0] || document.querySelector("audio") || null;
  }

  function attachListeners() {
    if (!media) return;
    bindMediaEvents(media);
    sendVideoInfo();
    sendStateUpdate();
    startUrlObserver();
  }

  function startUrlObserver() {
    if (urlObserver) return;
    // SPAs (YouTube, Spotify, etc.) navigate without a full reload. Re-bind
    // to the new media element when the URL changes.
    urlObserver = new MutationObserver(() => {
      if (location.href === lastUrl) return;
      lastUrl = location.href;
      setTimeout(rebindMedia, 800);
    });
    urlObserver.observe(document.body, { childList: true, subtree: true });
  }

  function rebindMedia() {
    const previous = media;
    findMedia();
    if (media && media !== previous) {
      if (previous) unbindMediaEvents(previous);
      bindMediaEvents(media);
    }
    sendVideoInfo();
    sendStateUpdate();
  }

  function sendVideoInfo() {
    const info = {
      type: "media-video-info",
      url: location.href,
      title: getMediaTitle(),
      hasMedia: !!media,
    };
    chrome.runtime.sendMessage(info).catch(() => {});
  }

  function sendStateUpdate() {
    if (!media) {
      chrome.runtime
        .sendMessage({
          type: "media-state-update",
          playing: false,
          time: 0,
          duration: 0,
        })
        .catch(() => {});
      return;
    }
    const msg = {
      type: "media-state-update",
      playing: !media.paused && !media.ended,
      time: media.currentTime || 0,
      duration: Number.isFinite(media.duration) ? media.duration : 0,
    };
    chrome.runtime.sendMessage(msg).catch(() => {});
  }

  function getMediaTitle() {
    // YouTube watch + shorts: the title lives in a known element. Falling
    // back to document.title catches everything else — most sites surface
    // the media title in <title> already.
    const ytWatch = document.querySelector(
      "#above-the-fold h1.ytd-watch-metadata yt-formatted-string",
    );
    if (ytWatch) return ytWatch.textContent.trim();

    const ytShortsTitle = document.querySelector(
      "ytd-reel-video-renderer[is-active] yt-shorts-video-title-view-model, " +
        "ytd-reel-video-renderer[is-active] h2",
    );
    if (ytShortsTitle) return ytShortsTitle.textContent.trim();

    // Open Graph title is the convention many media sites follow.
    const og = document.querySelector('meta[property="og:title"]');
    if (og?.content) return og.content.trim();

    return (document.title || location.hostname || "").trim();
  }

  // Direct queries from the side panel (faster than polling the cache).
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "get-video-info") {
      sendResponse({
        type: "media-video-info",
        url: location.href,
        title: getMediaTitle(),
        hasMedia: !!media,
      });
      return false;
    }

    if (msg.type === "get-video-state") {
      if (!media) {
        sendResponse({
          type: "media-state-update",
          playing: false,
          time: 0,
          duration: 0,
        });
      } else {
        sendResponse({
          type: "media-state-update",
          playing: !media.paused && !media.ended,
          time: media.currentTime || 0,
          duration: Number.isFinite(media.duration) ? media.duration : 0,
        });
      }
      return false;
    }
  });

  // Tear observers down on page unload. SPA navigations stay within the
  // same content-script instance, but a hard navigation / tab close can
  // race observer callbacks with the dying document — disconnecting
  // explicitly avoids the "operation on detached document" warnings.
  window.addEventListener("pagehide", () => {
    bodyObserver?.disconnect();
    urlObserver?.disconnect();
    if (media) unbindMediaEvents(media);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
