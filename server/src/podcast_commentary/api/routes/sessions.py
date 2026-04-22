import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from livekit import api
from pydantic import BaseModel

from podcast_commentary.core.config import settings
from podcast_commentary.core.db import (
    create_session,
    end_session,
    get_session,
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


@router.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session_route(request: CreateSessionRequest):
    room_name = f"commentary-{uuid4().hex[:8]}"
    user_identity = f"user-{uuid4().hex[:8]}"

    session_id = await create_session(room_name, request.video_url, request.video_title)

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


@router.get("/health")
async def health():
    return {"status": "ok"}
