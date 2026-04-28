/**
 * RoomController — owns a single LiveKit Room instance and its event
 * handlers. Encapsulates the connect/dispose lifecycle so callers can't
 * accidentally publish on a half-torn-down room or forget to remove
 * listeners before disconnect (the closures would otherwise hold stale
 * references to the per-session state).
 *
 * Event handlers are passed in as a flat object — callers wire their own
 * concerns (audio routing, UI state, captions) without RoomController
 * needing to know what they do.
 */

import { ConnectionState, Room, RoomEvent } from "livekit-client";

export class RoomController {
  constructor(handlers = {}) {
    this._handlers = handlers;
    this._room = null;
  }

  async connect(token, livekitUrl) {
    this._room = new Room({ adaptiveStream: true, dynacast: true });

    const h = this._handlers;
    const wire = (event, handler) => handler && this._room.on(event, handler);
    wire(RoomEvent.TrackSubscribed, h.onTrackSubscribed);
    wire(RoomEvent.TrackUnsubscribed, h.onTrackUnsubscribed);
    wire(RoomEvent.DataReceived, h.onDataReceived);
    wire(RoomEvent.ActiveSpeakersChanged, h.onActiveSpeakers);
    wire(RoomEvent.ConnectionStateChanged, h.onConnectionState);
    wire(RoomEvent.Disconnected, h.onDisconnected);
    wire(RoomEvent.ParticipantConnected, h.onParticipantConnected);
    wire(RoomEvent.ParticipantDisconnected, h.onParticipantDisconnected);

    await this._room.connect(livekitUrl, token);
    console.log("[ext] Connected to LiveKit room");
  }

  async dispose() {
    if (!this._room) return;
    const prior = this._room;
    this._room = null;
    // Drop our handlers before disconnecting so the closures over the
    // now-null room and the participant sets don't fire on the
    // teardown's own disconnect events.
    try {
      prior.removeAllListeners();
    } catch {}
    try {
      // `disconnect(true)` waits for the LiveKit transport to actually
      // close — awaiting it prevents a new Start from racing with the
      // old room's teardown.
      await prior.disconnect(true);
    } catch (err) {
      console.warn("[ext] room.disconnect raised:", err);
    }
  }

  isConnected() {
    return this._room?.state === ConnectionState.Connected;
  }

  get room() {
    return this._room;
  }

  get localParticipantIdentity() {
    return this._room?.localParticipant?.identity ?? null;
  }

  async publishTrack(mediaStreamTrack, options) {
    if (!this._room) throw new Error("Room not connected");
    return this._room.localParticipant.publishTrack(mediaStreamTrack, options);
  }

  // Best-effort fire-and-forget JSON publish on a topic. Silently
  // no-ops when not connected so callers (pacing UI, skip button) don't
  // need to gate every call site.
  async publishControl(payload, topic) {
    if (!this.isConnected()) return;
    try {
      const encoder = new TextEncoder();
      await this._room.localParticipant.publishData(encoder.encode(JSON.stringify(payload)), {
        reliable: true,
        topic,
      });
    } catch (err) {
      console.warn("[ext] publishData failed:", err);
    }
  }
}
