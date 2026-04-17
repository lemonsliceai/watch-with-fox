import json
import logging
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from livekit import api
from pydantic import BaseModel

from podcast_commentary.core.config import settings
from podcast_commentary.core.db import (
    create_session,
    end_session,
    get_session,
    get_session_audio_url,
)

logger = logging.getLogger("podcast-commentary.sessions")
router = APIRouter()


class CreateSessionRequest(BaseModel):
    video_url: str
    video_title: str | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    room_name: str
    token: str
    livekit_url: str
    video_url: str
    audio_url: str | None = None


@router.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session_route(request: CreateSessionRequest, req: Request):
    room_name = f"commentary-{uuid4().hex[:8]}"
    user_identity = f"user-{uuid4().hex[:8]}"

    # Insert the session row first so we can pass its id to the agent. The
    # agent uses session_id to persist conversation messages back to the
    # same row (transcripts, replies, summaries).
    session_id = await create_session(
        room_name, request.video_url, request.video_title, audio_stream_url=None
    )

    # NOTE: yt-dlp extraction happens inside the agent process (not here).
    # YouTube's signed URLs are bound to the requester's IP, so if the API
    # server extracts the URL, ffmpeg on the agent worker (different IP) gets
    # a 403. Letting the agent extract means the signed URL's ip= param
    # matches the process that ultimately fetches it.
    metadata = {
        "session_id": session_id,
        "video_url": request.video_url,
        "video_title": request.video_title or "",
        "avatar_url": settings.AVATAR_URL,
    }

    token = (
        api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(user_identity)
        .with_name(user_identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=settings.AGENT_NAME,
                        metadata=json.dumps(metadata),
                    )
                ],
            )
        )
        .to_jwt()
    )

    logger.info("Created session %s in room %s", session_id, room_name)

    return CreateSessionResponse(
        session_id=session_id,
        room_name=room_name,
        token=token,
        livekit_url=settings.LIVEKIT_URL,
        video_url=request.video_url,
        audio_url=None,
    )


@router.get("/api/sessions/{session_id}")
async def get_session_route(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/api/sessions/{session_id}/end")
async def end_session_route(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await end_session(session_id)
    return {"status": "ended"}


@router.get("/api/audio-stream/{session_id}")
async def audio_stream(session_id: str, request: Request):
    """Proxy YouTube audio to avoid CORS issues with googlevideo.com CDN.

    The browser's Web Audio API requires CORS headers to capture audio from
    cross-origin <audio> elements. YouTube's CDN doesn't send these headers,
    so we proxy the audio through our server which has CORSMiddleware.
    """
    raw_url = await get_session_audio_url(session_id)
    if not raw_url:
        raise HTTPException(status_code=404, detail="Audio stream not found")

    # Forward Range header for seeking support
    upstream_headers: dict[str, str] = {}
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=None))
    try:
        upstream = await client.send(
            client.build_request("GET", raw_url, headers=upstream_headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("Audio proxy upstream error: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to fetch audio stream")

    async def stream_audio():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    if "content-range" in upstream.headers:
        response_headers["Content-Range"] = upstream.headers["content-range"]
    if "content-length" in upstream.headers:
        response_headers["Content-Length"] = upstream.headers["content-length"]

    return StreamingResponse(
        stream_audio(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "audio/mp4"),
        headers=response_headers,
    )


@router.get("/health")
async def health():
    return {"status": "ok"}
