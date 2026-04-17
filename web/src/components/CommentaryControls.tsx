"use client";

interface CommentaryControlsProps {
  isPlaying: boolean;
  commentaryEnabled: boolean;
  videoVolume: number;
  commentaryVolume: number;
  isTalking: boolean;
  onTogglePlay: () => void;
  onToggleCommentary: () => void;
  onVideoVolumeChange: (volume: number) => void;
  onCommentaryVolumeChange: (volume: number) => void;
  onTalkStart: () => void;
  onTalkEnd: () => void;
  onDisconnect: () => void;
}

export default function CommentaryControls({
  isPlaying,
  commentaryEnabled,
  videoVolume,
  commentaryVolume,
  isTalking,
  onTogglePlay,
  onToggleCommentary,
  onVideoVolumeChange,
  onCommentaryVolumeChange,
  onTalkStart,
  onTalkEnd,
  onDisconnect,
}: CommentaryControlsProps) {
  return (
    <div className="flex items-center justify-between px-6 py-3.5 backdrop-blur-md bg-deep/85 border-t border-edge">
      <div className="flex items-center gap-7">
        {/* Play/Pause */}
        <button
          onClick={onTogglePlay}
          className="text-warm hover:text-accent transition-all duration-200 cursor-pointer"
        >
          {isPlaying ? (
            <svg className="w-6 h-6" fill="currentColor" viewBox="0 0 24 24">
              <path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z" />
            </svg>
          ) : (
            <svg className="w-6 h-6" fill="currentColor" viewBox="0 0 24 24">
              <path d="M8 5v14l11-7z" />
            </svg>
          )}
        </button>

        {/* Video Volume */}
        <div className="flex items-center gap-2.5">
          <span className="text-xs w-12 font-medium text-secondary">Video</span>
          <input
            type="range"
            min={0}
            max={100}
            value={videoVolume}
            onChange={(e) => onVideoVolumeChange(Number(e.target.value))}
            className="w-24"
          />
        </div>

        {/* Commentary Volume */}
        <div className="flex items-center gap-2.5">
          <span className="text-xs w-16 font-medium italic font-serif text-accent">
            Fox
          </span>
          <input
            type="range"
            min={0}
            max={100}
            value={commentaryVolume}
            onChange={(e) => onCommentaryVolumeChange(Number(e.target.value))}
            className="w-24"
          />
        </div>
      </div>

      <div className="flex items-center gap-4">
        {/* Hold to Talk
            Uses Pointer Events with pointer capture (per MDN + react-spectrum
            guidance). setPointerCapture on pointerdown retargets all subsequent
            events for this pointer to the button — so pointerup ALWAYS fires on
            this element even if the user drags off. onPointerLeave is NOT used
            because it fired stray "end" events on rerender (button scales up on
            talk start), leaving the mic/audio stuck muted after release. */}
        <button
          onPointerDown={(e) => {
            e.currentTarget.setPointerCapture(e.pointerId);
            onTalkStart();
          }}
          onPointerUp={(e) => {
            // Capture is auto-released on pointerup, but call explicitly to be
            // safe across browsers (some retain capture if released manually).
            try {
              e.currentTarget.releasePointerCapture(e.pointerId);
            } catch {
              // Already released — ignore.
            }
            onTalkEnd();
          }}
          onPointerCancel={(e) => {
            // System cancel (phone call, alt-tab mid-press, OS gesture): still
            // unmute so the user isn't stuck silent.
            try {
              e.currentTarget.releasePointerCapture(e.pointerId);
            } catch {
              // Already released — ignore.
            }
            onTalkEnd();
          }}
          onContextMenu={(e) => e.preventDefault()}
          style={{ touchAction: "none" }}
          className={`flex items-center gap-2 px-4 py-1.5 rounded-soft text-sm font-medium transition-all duration-200 cursor-pointer select-none ${
            isTalking
              ? "bg-accent text-white shadow-[0_0_16px_rgba(212,132,90,0.4)] scale-105"
              : "bg-elevated text-secondary hover:text-warm hover:bg-elevated/80"
          }`}
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z" />
          </svg>
          {isTalking ? "Listening..." : "Hold to talk"}
        </button>

        {/* Commentary Toggle */}
        <button
          onClick={onToggleCommentary}
          className={`px-4 py-1.5 rounded-soft text-sm font-medium transition-all duration-300 cursor-pointer ${
            commentaryEnabled
              ? "bg-accent text-white shadow-[0_0_12px_rgba(212,132,90,0.25)]"
              : "bg-elevated text-muted"
          }`}
        >
          Commentary {commentaryEnabled ? "ON" : "OFF"}
        </button>

        {/* Disconnect */}
        <button
          onClick={onDisconnect}
          className="px-4 py-1.5 rounded-soft text-sm font-medium bg-danger/15 text-danger hover:bg-danger/25 transition-all duration-300 cursor-pointer"
        >
          End Session
        </button>
      </div>
    </div>
  );
}
