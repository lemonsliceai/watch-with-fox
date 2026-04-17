"use client";

import { useEffect, useRef, useState } from "react";
import {
  Room,
  RoomEvent,
  Track,
  RemoteTrack,
  RemoteTrackPublication,
  RemoteParticipant,
} from "livekit-client";

interface AvatarSidebarProps {
  room: Room | null;
}

export default function AvatarSidebar({ room }: AvatarSidebarProps) {
  const videoContainerRef = useRef<HTMLDivElement>(null);
  const [captions, setCaptions] = useState<string[]>([]);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    if (!room) return;

    const handleTrackSubscribed = (
      track: RemoteTrack,
      publication: RemoteTrackPublication,
      participant: RemoteParticipant
    ) => {
      // Check if this is the avatar participant
      const isAvatar =
        participant.identity === "lemonslice-avatar-agent" ||
        participant.attributes?.["lk.publish_on_behalf"];

      if (!isAvatar) return;

      if (track.kind === Track.Kind.Video && videoContainerRef.current) {
        const el = track.attach();
        el.style.width = "100%";
        el.style.height = "100%";
        // Container is 2:3 to match the LemonSlice avatar stream. Use "cover"
        // so any sub-pixel rounding crops rather than exposing a hairline bar.
        el.style.objectFit = "cover";
        el.style.borderRadius = "16px";
        videoContainerRef.current.innerHTML = "";
        videoContainerRef.current.appendChild(el);
        setIsConnected(true);
      }

      // Audio is handled by RoomAudioRenderer in the parent — no manual attachment needed
    };

    const handleTrackUnsubscribed = (track: RemoteTrack) => {
      track.detach().forEach((el) => el.remove());
    };

    // Listen for agent text (captions)
    const handleDataReceived = (
      payload: Uint8Array,
      participant?: RemoteParticipant
    ) => {
      try {
        const msg = JSON.parse(new TextDecoder().decode(payload));
        if (msg.type === "agent_transcript" || msg.text) {
          const text = msg.text || msg.content;
          if (text) {
            setCaptions((prev) => [...prev.slice(-4), text]);
          }
        }
      } catch {
        // Not JSON, ignore
      }
    };

    room.on(RoomEvent.TrackSubscribed, handleTrackSubscribed);
    room.on(RoomEvent.TrackUnsubscribed, handleTrackUnsubscribed);
    room.on(RoomEvent.DataReceived, handleDataReceived);

    return () => {
      room.off(RoomEvent.TrackSubscribed, handleTrackSubscribed);
      room.off(RoomEvent.TrackUnsubscribed, handleTrackUnsubscribed);
      room.off(RoomEvent.DataReceived, handleDataReceived);
    };
  }, [room]);

  return (
    <div
      className={`relative w-full h-full overflow-hidden bg-surface rounded-soft border border-edge transition-all duration-700 ${isConnected ? "animate-gentle-breathe" : ""}`}
    >
      {/* Loading state */}
      {!isConnected && (
        <div className="absolute inset-0 flex items-center justify-center z-10">
          <div className="text-center space-y-4">
            <div className="w-12 h-12 rounded-full border-[2.5px] border-accent/15 border-t-accent animate-warm-spin mx-auto" />
            <p className="text-sm text-secondary animate-warm-pulse">
              Fox is joining...
            </p>
          </div>
        </div>
      )}

      {/* Avatar video */}
      <div ref={videoContainerRef} className="w-full h-full" />

      {/* Name badge */}
      {isConnected && (
        <div className="absolute top-3 left-3 backdrop-blur-md px-3.5 py-1.5 z-10 bg-deep/85 rounded-soft border border-edge">
          <span className="text-sm font-medium italic font-serif text-accent">
            Fox
          </span>
        </div>
      )}

      {/* Commentary captions overlay */}
      {captions.length > 0 && (
        <div
          className="absolute bottom-0 left-0 right-0 p-4 z-10"
          style={{ background: "linear-gradient(to top, rgba(26, 26, 26, 0.85) 0%, rgba(26, 26, 26, 0.6) 60%, transparent 100%)" }}
        >
          <div className="space-y-1.5">
            {captions.map((caption, i) => (
              <p
                key={i}
                className={`text-sm leading-relaxed ${
                  i === captions.length - 1
                    ? "text-white animate-fade-slide-up"
                    : "text-white/60"
                } transition-colors duration-400`}
                style={{ textShadow: "0 1px 4px rgba(0,0,0,0.5)" }}
              >
                {caption}
              </p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
