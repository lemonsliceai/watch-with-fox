const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

export interface SessionResponse {
  session_id: string;
  room_name: string;
  token: string;
  livekit_url: string;
  video_url: string;
  audio_url: string | null;
}

export async function createSession(
  videoUrl: string,
  videoTitle?: string
): Promise<SessionResponse> {
  const res = await fetch(`${API_URL}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_url: videoUrl, video_title: videoTitle }),
  });
  if (!res.ok) {
    throw new Error(`Failed to create session: ${res.statusText}`);
  }
  return res.json();
}

export async function endSession(sessionId: string): Promise<void> {
  await fetch(`${API_URL}/api/sessions/${sessionId}/end`, { method: "POST" });
}
