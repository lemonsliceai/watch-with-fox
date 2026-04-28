/**
 * Tab audio capture — request a stream ID from the background service
 * worker, materialize it into a MediaStream, and publish it to LiveKit.
 *
 * The background worker is the one with `tabCapture` permission, so the
 * side panel has to ask it for a stream ID and then call getUserMedia
 * with the magic chromeMediaSource constraints to actually get the
 * audio.
 */

import { Track } from "livekit-client";

// Ask the background worker for a tab-capture stream ID.
function requestStreamId(tabId) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type: "capture-tab-audio", tabId }, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!resp || resp.error) {
        reject(new Error(resp?.error || "Failed to capture tab audio"));
        return;
      }
      resolve(resp);
    });
  });
}

// Materialize the stream ID into a MediaStream. Disable echoCancellation /
// noiseSuppression / autoGainControl — getUserMedia turns these on by
// default and AGC in particular quietly attenuates loud tab audio to
// normalize loudness, perceived as a small volume drop the moment capture
// starts. Turning them off keeps the loopback bit-perfect so tab volume
// stays put, giving the sidechain duck a stable reference level to ramp
// down from.
async function getTabMediaStream(streamId) {
  return navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId,
      },
      optional: [
        { echoCancellation: false },
        { noiseSuppression: false },
        { autoGainControl: false },
      ],
    },
  });
}

/**
 * End-to-end: capture the active tab's audio, route it through the audio
 * graph for sidechain ducking + local loopback, and publish it to LiveKit
 * for the agent's STT pipeline.
 *
 * Returns the captured MediaStream so the caller can stop its tracks
 * during teardown (the audio graph's own teardown closes the context but
 * doesn't own the stream's lifecycle).
 */
export async function captureAndPublishTabAudio({ tabId, room, audioGraph }) {
  const { streamId } = await requestStreamId(tabId);
  const stream = await getTabMediaStream(streamId);

  const audioTracks = stream.getAudioTracks();
  if (audioTracks.length === 0) {
    throw new Error("No audio tracks in tab capture stream");
  }

  // Route the captured audio back to the user's speakers. tabCapture
  // intercepts the tab's audio output — without this loopback the page
  // would appear to mute the moment we start capturing. Going through
  // tabDuckGain means the sidechain follower can drive ducking off
  // persona voice energy.
  const wired = audioGraph.attachTabStream(stream);
  if (!wired) {
    // initAudioGraph runs before this in startSession, but if something
    // tore it down concurrently (End clicked mid-start), publish the
    // raw stream regardless. The tab is inaudible locally, but the
    // agent still gets STT.
    console.warn("[ext] Audio graph missing — tab audio loopback skipped");
  }

  // `Source.ScreenShareAudio` is the semantically correct source for
  // captured tab/window audio. Using it (instead of Unknown) ensures
  // LiveKit auto-subscribe works reliably — the agent's room-level
  // track_subscribed handler then matches on `name === "podcast-audio"`
  // and attaches it to the STT pipeline.
  const publication = await room.publishTrack(audioTracks[0], {
    name: "podcast-audio",
    source: Track.Source.ScreenShareAudio,
  });

  console.log(
    "[ext] Published podcast-audio:",
    "sid=",
    publication?.trackSid,
    "kind=",
    publication?.kind,
    "source=",
    publication?.source,
    "muted=",
    audioTracks[0].muted,
    "readyState=",
    audioTracks[0].readyState,
  );

  // If the track goes muted / ends unexpectedly, surface it. Helps
  // diagnose cases where tabCapture succeeds but silently stops
  // producing audio (e.g. user switched tabs or the tab was closed).
  audioTracks[0].addEventListener("mute", () => console.warn("[ext] podcast-audio track muted"));
  audioTracks[0].addEventListener("ended", () => console.warn("[ext] podcast-audio track ended"));

  return stream;
}

// Stop every track in the captured stream. Owned separately from the
// audio graph because the stream's lifecycle is independent of the
// AudioContext (closing the context doesn't stop the underlying
// MediaStreamTrack).
export function stopTabStream(stream) {
  if (!stream) return;
  for (const t of stream.getTracks()) t.stop();
}
