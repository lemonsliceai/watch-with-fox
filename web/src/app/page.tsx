"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createSession } from "@/lib/api";

function extractYouTubeId(url: string): string | null {
  const match = url.match(
    /(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([^&?/]+)/
  );
  return match ? match[1] : null;
}

export default function HomePage() {
  const [videoUrl, setVideoUrl] = useState("https://www.youtube.com/watch?v=TN2RmNuX4-k&t=3133s");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const router = useRouter();

  const handleStart = async () => {
    if (!videoUrl.trim()) {
      setError("Please enter a video URL");
      return;
    }

    setLoading(true);
    setError("");

    try {
      const session = await createSession(videoUrl.trim());
      const params = new URLSearchParams({
        sessionId: session.session_id,
        roomName: session.room_name,
        token: session.token,
        livekitUrl: session.livekit_url,
        videoUrl: session.video_url,
        ...(session.audio_url ? { audioUrl: session.audio_url } : {}),
      });
      router.push(`/watch?${params.toString()}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start session");
    } finally {
      setLoading(false);
    }
  };

  const ytId = extractYouTubeId(videoUrl);

  return (
    <main
      className="flex-1 flex flex-col items-center justify-center px-6 py-20 bg-deep"
    >
      <div className="max-w-4xl w-full space-y-10">
        {/* Header */}
        <div className="text-center space-y-4">
          <h1 className="text-5xl font-bold tracking-tight text-warm">
            Watch with{" "}
            <span className="font-serif italic text-accent">Fox</span>
          </h1>
          <p className="text-lg leading-relaxed text-secondary">
            Never watch YouTube alone again
          </p>
        </div>

        {/* URL Input */}
        <div className="space-y-5">
          <div className="flex gap-3">
            <input
              type="url"
              value={videoUrl}
              onChange={(e) => {
                setVideoUrl(e.target.value);
                setError("");
              }}
              placeholder="Paste a YouTube or video URL..."
              className="flex-1 px-5 py-3.5 text-base bg-surface text-warm border border-edge rounded-soft shadow-warm-sm placeholder:text-faint focus:outline-none focus:border-accent focus:shadow-[0_0_0_3px_rgba(212,132,90,0.15)] transition-all duration-300"
              onKeyDown={(e) => e.key === "Enter" && handleStart()}
            />
            <button
              onClick={handleStart}
              disabled={loading}
              className={`px-7 py-3.5 font-medium rounded-soft transition-all duration-300 cursor-pointer ${
                loading
                  ? "bg-elevated text-muted opacity-40 cursor-not-allowed"
                  : "bg-accent text-white shadow-warm hover:bg-accent-hover hover:-translate-y-px hover:shadow-warm-lg"
              }`}
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <span className="inline-block w-4 h-4 rounded-full border-2 border-muted border-t-transparent animate-warm-spin" />
                  Starting...
                </span>
              ) : (
                "Watch with Fox"
              )}
            </button>
          </div>

          {error && <p className="text-sm text-danger">{error}</p>}

          {/* Thumbnail + Fox side-by-side (matches watch page layout) */}
          {ytId && (
            <div className="flex flex-col md:flex-row gap-3">
              <div className="flex-[3] min-w-0 overflow-hidden rounded-soft border border-edge shadow-warm-lg transition-all duration-500">
                <img
                  src={`https://img.youtube.com/vi/${ytId}/maxresdefault.jpg`}
                  alt="Video thumbnail"
                  className="w-full aspect-video object-cover"
                />
              </div>
              <div className="aspect-[2/3] md:flex-[1] min-w-0 overflow-hidden rounded-soft border border-edge shadow-warm-lg">
                <img
                  src="/fox_2x3.jpg"
                  alt="Fox"
                  className="w-full h-full object-cover"
                />
              </div>
            </div>
          )}
        </div>

        {/* How it works */}
        <div className="grid grid-cols-3 gap-5 pt-4">
          {[
            { step: "1", title: "Paste a URL", desc: "YouTube, Vimeo, or any video link" },
            { step: "2", title: "Watch together", desc: "Fox listens and reacts in real time" },
            { step: "3", title: "Enjoy the show", desc: "Comedic commentary as you watch" },
          ].map((item) => (
            <div
              key={item.step}
              className="text-center space-y-3 p-5 bg-surface border border-edge rounded-soft hover:border-edge-warm hover:shadow-warm transition-all duration-300"
            >
              <div className="text-xl font-bold mx-auto flex items-center justify-center w-9 h-9 rounded-full bg-accent/15 text-accent">
                {item.step}
              </div>
              <h3 className="font-medium text-sm text-warm">{item.title}</h3>
              <p className="text-xs leading-relaxed text-muted">{item.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </main>
  );
}
