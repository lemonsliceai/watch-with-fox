"use client";

import { useEffect, useRef, useState, useCallback, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  RoomEvent,
  DisconnectReason,
  ConnectionState,
  Track,
  type Participant,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
} from "livekit-client";
import {
  LiveKitRoom,
  useRoomContext,
  RoomAudioRenderer,
} from "@livekit/components-react";
import VideoPlayer, {
  YT_STATE,
  type VideoPlayerHandle,
} from "@/components/VideoPlayer";
import AvatarSidebar from "@/components/AvatarSidebar";
import CommentaryControls from "@/components/CommentaryControls";

/** Inner component — rendered inside <LiveKitRoom>, so useRoomContext() is available. */
function WatchInner({ videoUrl }: { videoUrl: string }) {
  const room = useRoomContext();
  const router = useRouter();

  const [isPlaying, setIsPlaying] = useState(true);
  const [commentaryEnabled, setCommentaryEnabled] = useState(true);
  const [videoVolume, setVideoVolume] = useState(80);
  const [commentaryVolume, setCommentaryVolume] = useState(100);
  const [isTalking, setIsTalking] = useState(false);
  const [ducking, setDucking] = useState(false);

  const isTalkingRef = useRef(false);

  // Imperative handle to the YouTube iframe. Lets us query the current
  // playhead + state when the avatar becomes ready mid-playback.
  const videoPlayerRef = useRef<VideoPlayerHandle>(null);

  // Track the latest podcast playhead intent so we can (a) drop sends
  // before the room is connected, and (b) re-emit once the room connects so
  // the agent's ffmpeg catches up to whatever the iframe is doing.
  const podcastIntentRef = useRef<
    { type: "play"; t: number } | { type: "pause" } | null
  >(null);

  // Once we've synced the playhead to the agent (triggered by the avatar's
  // video track going live), don't keep resending on every subsequent
  // subscription — user actions (play/pause) take over from there.
  const syncedToAgentRef = useRef(false);

  // Distinguish the first Connected transition (initial sync) from later
  // Connected transitions (reconnects — replay the last known intent).
  const hasConnectedOnceRef = useRef(false);

  // Effective volumes:
  // - While hold-to-talk is active, duck the video to the same low level we
  //   use when Fox is speaking (not full-mute — the user still wants to
  //   hear the podcast faintly while they comment on it).
  // - Fox's audio is muted locally during hold-to-talk so the user's
  //   mic doesn't re-capture his voice from the speakers and confuse STT.
  //   The mute is applied client-side via the volume-on-audio-elements
  //   effect below, so it restores immediately when isTalking flips back.
  const effectiveVideoVolume = isTalking || ducking ? 5 : videoVolume;
  const effectiveCommentaryVolume = isTalking ? 0 : commentaryVolume;

  // Duck the video whenever any remote participant is actively speaking (i.e.
  // Fox). Driving this off LiveKit's live audio-level detection means the
  // volume always restores the moment the voice stops — we don't depend on
  // the agent reliably sending a matching "commentary_end" data message.
  useEffect(() => {
    const localIdentity = room.localParticipant.identity;
    const handleActiveSpeakersChanged = (speakers: Participant[]) => {
      const remoteSpeaking = speakers.some((p) => p.identity !== localIdentity);
      setDucking(remoteSpeaking);
    };

    room.on(RoomEvent.ActiveSpeakersChanged, handleActiveSpeakersChanged);
    return () => {
      room.off(RoomEvent.ActiveSpeakersChanged, handleActiveSpeakersChanged);
    };
  }, [room]);

  // Small helper for sending JSON data-channel messages to the agent.
  // Returns true on success, false otherwise, and logs both branches so we
  // can diagnose send failures (the most common one being "room not yet
  // connected" on the very first YouTube onPlay).
  const publishControl = useCallback(
    async (payload: Record<string, unknown>, topic: string): Promise<boolean> => {
      if (room.state !== ConnectionState.Connected) {
        console.warn(
          `[control] skipped send topic=${topic}`,
          payload,
          `state=${room.state}`
        );
        return false;
      }
      try {
        const encoder = new TextEncoder();
        await room.localParticipant.publishData(
          encoder.encode(JSON.stringify(payload)),
          { reliable: true, topic }
        );
        console.log(`[control] sent topic=${topic}`, payload);
        return true;
      } catch (err) {
        console.warn(`[control] publishData failed topic=${topic}`, payload, err);
        return false;
      }
    },
    [room]
  );

  // --- Podcast playhead control -------------------------------------------
  // The server-side agent runs its own ffmpeg on the podcast URL and needs
  // play/pause events from the user's YouTube iframe to stay roughly in sync.
  //
  // The YouTube iframe's first `onPlay` typically fires before LiveKit has
  // finished the ICE handshake, so we keep a "latest intent" ref and re-send
  // it whenever the room transitions to Connected.

  const sendPodcastIntent = useCallback(
    (intent: { type: "play"; t: number } | { type: "pause" }) => {
      podcastIntentRef.current = intent;
      void publishControl(intent, "podcast.control");
    },
    [publishControl]
  );

  const handlePlaybackPlay = useCallback(
    (timeSec: number) => {
      sendPodcastIntent({ type: "play", t: timeSec });
    },
    [sendPodcastIntent]
  );

  const handlePlaybackPause = useCallback(() => {
    sendPodcastIntent({ type: "pause" });
  }, [sendPodcastIntent]);

  // --- Playhead sync to the agent -----------------------------------------
  // By design the YouTube iframe has been playing for several seconds before
  // the server agent is even dispatched. yt-dlp + ffmpeg + STT spin-up
  // takes another ~1–3 s after that. If we send `{type:"play", t:0}` on the
  // first YT onPlay, (a) it lands in an empty room — no agent participant
  // to receive it — and (b) even if it were received, ffmpeg would start at
  // t=0 while the user is already well past that.
  //
  // Handshake:
  //   • The agent publishes `{type:"agent_ready"}` on `commentary.control`
  //     once its podcast STT pipeline is armed — that is the authoritative
  //     signal that the server is ready to receive play/pause.
  //   • On agent_ready, query the YT player's current state/time and send
  //     it (with a small forward latency estimate so ffmpeg lands near the
  //     user's actual playhead).
  //   • Avatar-video-subscribed is kept as a fallback in case the
  //     agent_ready packet is missed (it fires slightly later but still
  //     proves the agent is in the room).
  //   • On reconnects, replay the last known intent. RoomConnected
  //     deliberately does NOT sync — the agent isn't in the room yet.
  const syncPlayheadToAgent = useCallback(
    (reason: string) => {
      if (syncedToAgentRef.current) return;
      const handle = videoPlayerRef.current;
      if (!handle) {
        console.warn(`[sync] ${reason} — no VideoPlayer handle yet, skipping`);
        return;
      }
      const state = handle.getPlayerState();
      const t = handle.getCurrentTime();
      console.log(
        `[sync] ${reason} → yt state=${state} t=${t.toFixed(2)}s`
      );

      // Forward estimate: client→LiveKit→agent + yt-dlp→ffmpeg spin-up +
      // decoder warm-up. In practice ~300–800 ms; 0.7 s is a safe centre.
      const SYNC_FORWARD_SEC = 0.7;

      if (state === YT_STATE.PLAYING || state === YT_STATE.BUFFERING) {
        syncedToAgentRef.current = true;
        sendPodcastIntent({
          type: "play",
          t: Math.max(0, t + SYNC_FORWARD_SEC),
        });
      } else if (state === YT_STATE.PAUSED) {
        syncedToAgentRef.current = true;
        sendPodcastIntent({ type: "pause" });
      }
      // UNSTARTED / CUED / ENDED: stay unsynced; the next YT state change
      // (onPlay/onPause) will sync via handlePlaybackPlay/Pause instead.
    },
    [sendPodcastIntent]
  );

  // Connection-state → Connected. On the very first connect we do NOT sync
  // — the agent worker is typically dispatched in response to this join and
  // hasn't attached its data_received handler yet, so a packet sent now
  // would be dropped silently. We wait for the `agent_ready` packet (see
  // below) instead. On subsequent Connected transitions (reconnects), we
  // replay the last known intent so ffmpeg restarts in the right state.
  useEffect(() => {
    const handleStateChange = (state: ConnectionState) => {
      console.log(`[room] connection state → ${state}`);
      if (state !== ConnectionState.Connected) return;
      if (!hasConnectedOnceRef.current) {
        hasConnectedOnceRef.current = true;
        // Intentionally no syncPlayheadToAgent here — see comment above.
      } else if (podcastIntentRef.current) {
        console.log(
          "[room] replaying podcast intent on reconnect",
          podcastIntentRef.current
        );
        void publishControl(podcastIntentRef.current, "podcast.control");
      }
    };
    room.on(RoomEvent.ConnectionStateChanged, handleStateChange);
    handleStateChange(room.state);
    return () => {
      room.off(RoomEvent.ConnectionStateChanged, handleStateChange);
    };
  }, [room, publishControl]);

  // Authoritative agent-ready handshake. The agent publishes
  // `{type:"agent_ready"}` on the `commentary.control` topic once its
  // podcast STT pipeline is initialised; that is the moment we know a
  // `{type:"play", t}` packet will actually land in its data_received
  // handler. We reset `syncedToAgentRef` first so a restarted agent
  // (same room, new worker) also gets a fresh sync.
  useEffect(() => {
    const onData = (
      payload: Uint8Array,
      _participant?: RemoteParticipant,
      _kind?: unknown,
      topic?: string
    ) => {
      if (topic !== "commentary.control") return;
      let msg: unknown;
      try {
        msg = JSON.parse(new TextDecoder().decode(payload));
      } catch {
        return;
      }
      if (
        typeof msg === "object" &&
        msg !== null &&
        (msg as { type?: unknown }).type === "agent_ready"
      ) {
        syncedToAgentRef.current = false;
        syncPlayheadToAgent("agent_ready");
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, syncPlayheadToAgent]);

  // Avatar video track subscribed → sync. This is the stronger signal: the
  // avatar is about to start its intro, which means the agent's pipeline is
  // fully alive. If we haven't synced yet (YT was not ready at room-connect
  // time), this query will catch the correct playhead.
  useEffect(() => {
    const onTrackSubscribed = (
      _track: RemoteTrack,
      publication: RemoteTrackPublication,
      participant: RemoteParticipant
    ) => {
      if (publication.kind !== Track.Kind.Video) return;
      if (!participant.identity.startsWith("lemonslice-")) return;
      syncPlayheadToAgent(
        `avatar video subscribed (from=${participant.identity})`
      );
    };

    room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);

    // Cover the StrictMode case where the avatar track is already
    // subscribed by the time this effect mounts.
    room.remoteParticipants.forEach((p) => {
      if (!p.identity.startsWith("lemonslice-")) return;
      p.videoTrackPublications.forEach((pub) => {
        if (pub.isSubscribed) {
          syncPlayheadToAgent(
            `avatar video already subscribed (from=${p.identity})`
          );
        }
      });
    });

    return () => {
      room.off(RoomEvent.TrackSubscribed, onTrackSubscribed);
    };
  }, [room, syncPlayheadToAgent]);

  // --- Hold-to-talk -------------------------------------------------------
  // The client no longer publishes a podcast-audio track. For hold-to-talk we
  // just enable the local mic (a fresh track) on press and disable on release;
  // the agent's `AgentSession` STT auto-subscribes.

  const handleTalkStart = useCallback(async () => {
    if (isTalkingRef.current) return;
    isTalkingRef.current = true;
    setIsTalking(true);

    await publishControl({ type: "user_talk_start" }, "user.control");

    try {
      await room.localParticipant.setMicrophoneEnabled(true);
    } catch (err) {
      console.error("Failed to enable mic:", err);
      isTalkingRef.current = false;
      setIsTalking(false);
      await publishControl({ type: "user_talk_end" }, "user.control");
    }
  }, [room, publishControl]);

  const handleTalkEnd = useCallback(async () => {
    if (!isTalkingRef.current) return;
    isTalkingRef.current = false;
    setIsTalking(false);

    // Signal the agent *immediately* — it starts its 1.5 s grace window
    // during which trailing STT finals are still accepted.
    await publishControl({ type: "user_talk_end" }, "user.control");

    // Delay the mic unpublish. VAD + turn detector need to observe trailing
    // silence to commit a turn; if we yank the track right away, short
    // utterances never produce an `on_user_turn_completed` event. We stay
    // inside the agent's grace window (1.5 s) with ~300 ms of safety margin.
    const DISABLE_MIC_DELAY_MS = 1200;
    setTimeout(() => {
      // Only disable if the user didn't press-and-hold again in the meantime.
      if (!isTalkingRef.current) {
        room.localParticipant.setMicrophoneEnabled(false).catch((err) => {
          console.warn("Failed to disable mic:", err);
        });
      }
    }, DISABLE_MIC_DELAY_MS);
  }, [room, publishControl]);

  // Apply the effective commentary volume to all remote audio tracks (Mr.
  // Fox's voice). When the user is holding to talk, this drops to 0 so the
  // mic doesn't re-capture Fox from the speakers.
  useEffect(() => {
    const vol = effectiveCommentaryVolume / 100;

    const applyVolume = () => {
      room.remoteParticipants.forEach((p) => {
        p.audioTrackPublications.forEach((pub) => {
          if (pub.track) {
            pub.track.attachedElements.forEach((el) => {
              el.volume = vol;
            });
          }
        });
      });
    };

    applyVolume();
    room.on(RoomEvent.TrackSubscribed, applyVolume);
    return () => {
      room.off(RoomEvent.TrackSubscribed, applyVolume);
    };
  }, [room, effectiveCommentaryVolume]);

  const handleDisconnect = () => {
    room.disconnect();
    router.push("/");
  };

  return (
    <div
      className="flex-1 flex flex-col h-screen bg-deep"
    >
      {/* Back button */}
      <div className="flex-none px-3 pt-3 md:px-4 md:pt-4">
        <button
          onClick={handleDisconnect}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-secondary hover:text-warm bg-surface/50 hover:bg-surface border border-edge rounded-soft transition-all duration-200 cursor-pointer"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="shrink-0">
            <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          New video
        </button>
      </div>

      {/* Main content — shared-height row on desktop, stacked on mobile.
          A container query computes a shared height for the video (16:9)
          and avatar (2:3) so each child fits within the available space at
          its natural aspect ratio — no letterboxing inside the iframe and
          no horizontal overflow regardless of viewport shape. */}
      <div className="flex-1 flex items-center justify-center p-3 md:p-4 min-h-0 overflow-hidden">
        <div
          className="w-full h-full max-w-[1600px] flex items-center justify-center"
          style={{ containerType: "size" }}
        >
          <div
            className="flex flex-col items-center justify-center gap-3 w-full md:flex-row md:w-auto"
            style={{
              // 22/9 = 16/9 (video) + 2/3 (avatar); subtract the 0.75rem gap.
              ["--shared-h" as string]:
                "min(100cqh, calc((100cqw - 0.75rem) * 9 / 22))",
            }}
          >
            {/* Video Player — 16:9 */}
            <div className="w-full aspect-video flex-none md:w-auto md:h-[var(--shared-h)]">
              <VideoPlayer
                ref={videoPlayerRef}
                videoUrl={videoUrl}
                videoVolume={effectiveVideoVolume}
                onPlaybackPlay={handlePlaybackPlay}
                onPlaybackPause={handlePlaybackPause}
                onPlay={() => setIsPlaying(true)}
                onPause={() => setIsPlaying(false)}
              />
            </div>

            {/* Avatar — 2:3 portrait matches LemonSlice video dimensions */}
            <div className="h-[320px] aspect-[2/3] flex-none mx-auto md:mx-0 md:w-auto md:h-[var(--shared-h)]">
              <AvatarSidebar room={room} />
            </div>
          </div>
        </div>
      </div>

      {/* Controls */}
      <CommentaryControls
        isPlaying={isPlaying}
        commentaryEnabled={commentaryEnabled}
        videoVolume={videoVolume}
        commentaryVolume={commentaryVolume}
        isTalking={isTalking}
        onTogglePlay={() => setIsPlaying(!isPlaying)}
        onToggleCommentary={() => setCommentaryEnabled(!commentaryEnabled)}
        onVideoVolumeChange={setVideoVolume}
        onCommentaryVolumeChange={setCommentaryVolume}
        onTalkStart={handleTalkStart}
        onTalkEnd={handleTalkEnd}
        onDisconnect={handleDisconnect}
      />

      {/* Renders audio from remote participants (Fox's voice) */}
      <RoomAudioRenderer />
    </div>
  );
}

function WatchContent() {
  const searchParams = useSearchParams();
  const router = useRouter();

  const token = searchParams.get("token") || "";
  const livekitUrl = searchParams.get("livekitUrl") || "";
  const videoUrl = searchParams.get("videoUrl") || "";

  const [connectionError, setConnectionError] = useState("");

  if (!videoUrl || !token) {
    return (
      <div className="flex-1 flex items-center justify-center bg-deep">
        <div className="text-center space-y-5">
          <p className="text-secondary">No session found</p>
          <button
            onClick={() => router.push("/")}
            className="px-5 py-2.5 font-medium cursor-pointer bg-accent text-white rounded-soft shadow-warm transition-all duration-300"
          >
            Go back
          </button>
        </div>
      </div>
    );
  }

  if (connectionError) {
    return (
      <div className="flex-1 flex items-center justify-center bg-deep">
        <div className="text-center space-y-5">
          <p className="text-danger">Connection error: {connectionError}</p>
          <button
            onClick={() => router.push("/")}
            className="px-5 py-2.5 font-medium cursor-pointer bg-accent text-white rounded-soft shadow-warm transition-all duration-300"
          >
            Go back
          </button>
        </div>
      </div>
    );
  }

  return (
    <LiveKitRoom
      serverUrl={livekitUrl}
      token={token}
      connect={true}
      options={{ adaptiveStream: true, dynacast: true }}
      onDisconnected={(reason) => {
        // Only navigate home for server-initiated disconnects, not React cleanup
        if (reason !== DisconnectReason.CLIENT_INITIATED) {
          router.push("/");
        }
      }}
      onError={(err) =>
        setConnectionError(
          err instanceof Error ? err.message : "Failed to connect"
        )
      }
    >
      <WatchInner videoUrl={videoUrl} />
    </LiveKitRoom>
  );
}

export default function WatchPage() {
  return (
    <Suspense
      fallback={
        <div className="flex-1 flex items-center justify-center bg-deep">
          <div className="flex flex-col items-center gap-4">
            <div className="w-10 h-10 rounded-full border-[2.5px] border-accent/15 border-t-accent animate-warm-spin" />
            <p className="text-sm text-muted">Setting up your session...</p>
          </div>
        </div>
      }
    >
      <WatchContent />
    </Suspense>
  );
}
