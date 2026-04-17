"use client";

import {
  useRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  forwardRef,
} from "react";
import YouTube, { YouTubeEvent, YouTubePlayer } from "react-youtube";

/** YT player state codes (YT.PlayerState). */
export const YT_STATE = {
  UNSTARTED: -1,
  ENDED: 0,
  PLAYING: 1,
  PAUSED: 2,
  BUFFERING: 3,
  CUED: 5,
} as const;

export interface VideoPlayerHandle {
  /** Seconds into the video; 0 if the player isn't ready yet. */
  getCurrentTime: () => number;
  /** One of `YT_STATE.*`; `UNSTARTED` if the player isn't ready yet. */
  getPlayerState: () => number;
}

interface VideoPlayerProps {
  videoUrl: string;
  videoVolume: number; // 0-100, applied via YouTube setVolume()
  onPlaybackPlay?: (timeSec: number) => void;
  onPlaybackPause?: () => void;
  onPlay?: () => void;
  onPause?: () => void;
}

function extractYouTubeId(url: string): string | null {
  const match = url.match(
    /(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([^&?/]+)/
  );
  return match ? match[1] : null;
}

/** Extract the "t" query/fragment param from a YouTube URL and return seconds. */
function extractStartSeconds(url: string): number | undefined {
  try {
    const u = new URL(url);
    const raw = u.searchParams.get("t");
    if (!raw) return undefined;
    // Handle "3133s" or "3133" (plain seconds)
    const plain = raw.match(/^(\d+)s?$/);
    if (plain) return Number(plain[1]);
    // Handle "1h2m3s" style
    let total = 0;
    const h = raw.match(/(\d+)h/);
    const m = raw.match(/(\d+)m/);
    const s = raw.match(/(\d+)s/);
    if (h) total += Number(h[1]) * 3600;
    if (m) total += Number(m[1]) * 60;
    if (s) total += Number(s[1]);
    return total > 0 ? total : undefined;
  } catch {
    return undefined;
  }
}

/**
 * YouTube player + minimal remote-control bridge.
 *
 * We do NOT capture audio in the browser anymore: the agent fetches the podcast
 * audio directly server-side (yt-dlp + ffmpeg → Whisper). The only thing this
 * component contributes to the agent's awareness is play/pause/seek events,
 * emitted as `onPlaybackPlay(t)` / `onPlaybackPause()` callbacks. The parent
 * forwards them over the LiveKit data channel so the agent's ffmpeg tracks
 * the user's YouTube playhead.
 */
const VideoPlayer = forwardRef<VideoPlayerHandle, VideoPlayerProps>(function VideoPlayer(
  {
    videoUrl,
    videoVolume,
    onPlaybackPlay,
    onPlaybackPause,
    onPlay,
    onPause,
  },
  ref
) {
  const ytPlayerRef = useRef<YouTubePlayer | null>(null);

  // Imperative handle for the parent (watch page) to query the current
  // playhead + play state on demand — used when the agent/avatar becomes
  // ready mid-playback and needs to be told where to start ffmpeg.
  useImperativeHandle(
    ref,
    () => ({
      getCurrentTime: () => {
        try {
          return ytPlayerRef.current?.getCurrentTime?.() ?? 0;
        } catch {
          return 0;
        }
      },
      getPlayerState: () => {
        try {
          return ytPlayerRef.current?.getPlayerState?.() ?? YT_STATE.UNSTARTED;
        } catch {
          return YT_STATE.UNSTARTED;
        }
      },
    }),
    []
  );

  const ytId = extractYouTubeId(videoUrl);
  const startSeconds = extractStartSeconds(videoUrl);

  const handleYtPlay = useCallback(() => {
    const t = ytPlayerRef.current?.getCurrentTime?.() ?? 0;
    console.log(`[yt] onPlay t=${t.toFixed(2)}s`);
    onPlaybackPlay?.(t);
    onPlay?.();
  }, [onPlaybackPlay, onPlay]);

  const handleYtPause = useCallback(() => {
    console.log("[yt] onPause");
    onPlaybackPause?.();
    onPause?.();
  }, [onPlaybackPause, onPause]);

  const handleYtReady = useCallback(
    (event: YouTubeEvent) => {
      ytPlayerRef.current = event.target;
      console.log("[yt] onReady — iframe ready");
      // Start unmuted and set initial volume (ducking state may already be active)
      event.target.unMute();
      event.target.setVolume(videoVolume);
    },
    [videoVolume]
  );

  const handleYtStateChange = useCallback((event: YouTubeEvent) => {
    // Data values: -1 unstarted, 0 ended, 1 playing, 2 paused, 3 buffering, 5 cued
    const labels: Record<number, string> = {
      [-1]: "unstarted",
      0: "ended",
      1: "playing",
      2: "paused",
      3: "buffering",
      5: "cued",
    };
    console.log(
      `[yt] onStateChange → ${labels[event.data as number] ?? event.data}`
    );
  }, []);

  const handleYtError = useCallback((event: YouTubeEvent) => {
    console.warn("[yt] onError", event.data);
  }, []);

  // Apply volume changes to the YouTube player.
  useEffect(() => {
    if (ytPlayerRef.current) {
      try {
        ytPlayerRef.current.setVolume(videoVolume);
      } catch {
        // Player may not be fully initialised yet; ignore.
      }
    }
  }, [videoVolume]);

  // YouTube player
  if (ytId) {
    return (
      <div className="w-full h-full overflow-hidden relative bg-[#0f0d0b] rounded-soft border border-edge shadow-warm-lg">
        <YouTube
          videoId={ytId}
          className="w-full h-full"
          iframeClassName="w-full h-full"
          opts={{
            width: "100%",
            height: "100%",
            playerVars: {
              autoplay: 1,
              modestbranding: 1,
              rel: 0,
              ...(startSeconds != null ? { start: startSeconds } : {}),
            },
          }}
          onReady={handleYtReady}
          onPlay={handleYtPlay}
          onPause={handleYtPause}
          onStateChange={handleYtStateChange}
          onError={handleYtError}
        />
      </div>
    );
  }

  // HTML5 video fallback (dev-only — the agent won't hear audio for non-YouTube
  // sources since the server-side yt-dlp path requires a YouTube URL).
  return (
    <div className="w-full h-full overflow-hidden bg-[#0f0d0b] rounded-soft border border-edge shadow-warm-lg">
      <video
        src={videoUrl}
        controls
        className="w-full h-full object-contain"
        onPlay={() => onPlay?.()}
        onPause={() => onPause?.()}
      />
    </div>
  );
});

export default VideoPlayer;
