/**
 * Side panel entry point — bootstraps the session orchestrator, wires up
 * DOM controls, and hooks browser lifecycle events.
 *
 * This file deliberately stays thin: every concern (audio graph, LiveKit
 * room, UI slots, pacing, content-script messaging) lives in its own
 * module, and SessionLifecycle is the single seam where they meet. If
 * this file grows beyond the entry-point role, push the new logic down
 * into the appropriate module.
 *
 * Bundled by esbuild into dist/sidepanel.js, which sidepanel.html loads.
 */

import { SessionState } from "./config.js";
import { detectActiveMedia, registerTabMessageListener } from "./messaging/tab-bridge.js";
import { SessionLifecycle } from "./session.js";
import { initPacingControls } from "./ui/pacing-controls.js";

const $ = (sel) => document.querySelector(sel);

document.addEventListener("DOMContentLoaded", () => {
  const session = new SessionLifecycle();

  // ── Initial active-tab detection ──
  detectActiveMedia({
    onPreview: ({ url, title }) => {
      const btn = $("#start-btn");
      btn.disabled = false;
      btn.dataset.videoUrl = url;
      btn.dataset.videoTitle = title || "";
    },
    onNoMedia: () => {
      const btn = $("#start-btn");
      btn.disabled = true;
      delete btn.dataset.videoUrl;
      delete btn.dataset.videoTitle;
    },
  })
    .then((tabId) => {
      if (tabId != null) session.setActiveTabId(tabId);
    })
    .catch((err) => console.warn("[ext] initial detectActiveMedia failed:", err));

  // ── Buttons ──
  $("#start-btn").addEventListener("click", () => {
    session.start().catch((err) => console.error("[ext] start threw:", err));
  });
  $("#end-btn").addEventListener("click", () => {
    session.end().catch((err) => console.error("[ext] end threw:", err));
  });
  $("#skip-btn").addEventListener("click", () => session.skipCommentary());

  // ── Pacing ──
  // Pacing changes are persisted by the controls module itself; here we
  // forward each change to the agent. Before the room connects the
  // publish silently no-ops — the next session starts with the saved
  // values via `agent_ready`.
  initPacingControls(() => session.publishPacing());

  // ── Content-script messages (filtered to the active tab) ──
  registerTabMessageListener(() => session.activeTabId, {
    "media-state-update": (msg) => session.handleMediaStateUpdate(msg),
    "media-video-info": (msg) => updateVideoPreview(msg),
  });

  // ── Side-panel lifecycle ──
  // The user can collapse the panel (hides the page without unloading)
  // or close it entirely (`pagehide`). Pause the rAF envelope follower
  // while hidden so we don't burn CPU on a panel the user can't see;
  // tear the session down on unload so a closed panel doesn't leave
  // a captured tab and a connected room dangling on the agent side.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) session.pauseFollower();
    else session.resumeFollower();
  });
  // `pagehide` is the reliable "this page is going away" event for
  // service-worker-hosted contexts; `beforeunload` doesn't always fire.
  // Fire-and-forget the teardown — the page is unloading, so awaiting
  // accomplishes nothing, but kicking off `room.disconnect(true)` lets
  // the signaling close packet make it out before the runtime tears
  // us down.
  window.addEventListener("pagehide", () => {
    if (session.state === SessionState.LIVE) {
      session.end().catch(() => {});
    }
  });
});

function updateVideoPreview(info) {
  if (!info?.url) return;
  const btn = $("#start-btn");
  btn.disabled = false;
  btn.dataset.videoUrl = info.url;
  btn.dataset.videoTitle = info.title || "";
}
