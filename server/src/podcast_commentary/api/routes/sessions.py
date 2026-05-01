import logging
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from livekit import api
from pydantic import BaseModel

from podcast_commentary.agent.fox_config import _resolve_persona_names, load_config
from podcast_commentary.api.livekit_dispatch import (
    DispatchMetadata,
    PersonaDescriptor,
    SecondaryRoomDispatch,
)
from podcast_commentary.api.livekit_tokens import mint_agent_token
from podcast_commentary.api.routes.personas import (
    PersonaManifestEntry,
    build_persona_manifest,
)
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
    # Stable per-install id minted by the extension and persisted in
    # chrome.storage.local. Lets pre-auth sessions be merged into a
    # Clerk user post-signup. Optional for back-compat with older
    # extension builds.
    anonymous_id: str | None = None


class RoomEntry(BaseModel):
    """One LiveKit room the extension should join for this session.

    Exactly one entry has ``role == "primary"``: its token carries the
    ``RoomAgentDispatch`` that triggers the agent worker. Secondary
    entries are plain participant tokens; the agent self-joins those
    rooms via metadata embedded in the primary dispatch.
    """

    persona: str
    room_name: str
    token: str
    role: Literal["primary", "secondary"]


class CreateSessionResponse(BaseModel):
    session_id: str
    livekit_url: str
    video_url: str
    rooms: list[RoomEntry]
    # Authoritative persona lineup for THIS session — same shape as
    # ``GET /api/personas``. The extension re-renders avatar slots from
    # this so the stack always matches what the server actually minted.
    personas: list[PersonaManifestEntry]


def _persona_room_name(session_id: str, persona: str) -> str:
    """Deterministic per-persona room name. Stable for the lifetime of a session."""
    return f"{session_id}-{persona}"


def _user_id_from_authorization(authorization: str | None) -> str | None:
    """Extract a Clerk user id from an Authorization header.

    Stub for the future Clerk integration: today this always returns
    None. When Clerk is wired up, verify the JWT here and return its
    ``sub`` claim. Keeping the seam in place now means the route and
    DB schema already carry user_id end-to-end — Clerk drop-in is a
    one-function change rather than a schema migration + plumbing pass.
    """
    return None


@router.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session_route(
    request: CreateSessionRequest,
    authorization: str | None = Header(default=None),
):
    user_id = _user_id_from_authorization(authorization)
    persona_names = _resolve_persona_names()
    # Primary is whichever persona is listed first in PERSONAS — that's
    # the room the LiveKit dispatch lands in and whose timing drives
    # cadence. Reorder PERSONAS to change it; no separate setting.
    primary_persona = persona_names[0]

    session_id = str(uuid4())
    user_identity = f"user-{uuid4().hex[:8]}"

    persona_to_room: dict[str, str] = {
        persona: _persona_room_name(session_id, persona) for persona in persona_names
    }
    primary_room_name = persona_to_room[primary_persona]

    # Persist the session row with the per-persona room mapping. The
    # legacy ``room_name`` column gets the primary room.
    await create_session(
        primary_room_name,
        request.video_url,
        request.video_title,
        rooms=persona_to_room,
        session_id=session_id,
        user_id=user_id,
        anonymous_id=request.anonymous_id,
    )

    # Per-persona startup data the agent uses to spin up an AgentSession
    # in whichever room the persona ends up in. Same shape for primary
    # and secondaries — only the room differs.
    personas_meta: list[PersonaDescriptor] = []
    for name in persona_names:
        cfg = load_config(name)
        personas_meta.append(
            PersonaDescriptor(
                name=name,
                label=cfg.persona.speaker_label or name,
                avatar_url=cfg.avatar.avatar_url,
            )
        )

    # Mint one ``agent: true`` JWT per secondary room so the dispatched
    # agent worker can self-join those rooms without a round-trip back
    # to the API. The primary persona is intentionally omitted — its
    # room is the dispatched job's ``ctx.room`` and the worker is
    # already bound to it.
    secondary_rooms_meta: list[SecondaryRoomDispatch] = [
        SecondaryRoomDispatch(
            persona=name,
            room_name=persona_to_room[name],
            agent_token=mint_agent_token(
                persona_to_room[name],
                f"agent-{name}-{session_id}",
            ),
        )
        for name in persona_names
        if name != primary_persona
    ]

    dispatch_metadata = DispatchMetadata(
        session_id=session_id,
        video_url=request.video_url,
        video_title=request.video_title or "",
        primary_persona=primary_persona,
        all_personas=list(persona_names),
        secondary_rooms=secondary_rooms_meta,
        personas=personas_meta,
    )

    # One entry per persona so the extension spawns a RoomController per
    # room.
    rooms: list[RoomEntry] = []
    for persona in persona_names:
        room_name = persona_to_room[persona]
        is_primary = persona == primary_persona

        builder = (
            api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
            .with_identity(user_identity)
            .with_name(user_identity)
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
        )
        if is_primary:
            # RoomAgentDispatch lives ONLY on the primary token. Its
            # metadata carries everything the agent needs to spin up
            # secondary rooms.
            builder = builder.with_room_config(
                api.RoomConfiguration(
                    agents=[
                        api.RoomAgentDispatch(
                            agent_name=settings.AGENT_NAME,
                            metadata=dispatch_metadata.to_metadata_json(),
                        )
                    ],
                )
            )

        rooms.append(
            RoomEntry(
                persona=persona,
                room_name=room_name,
                token=builder.to_jwt(),
                role="primary" if is_primary else "secondary",
            )
        )

    logger.info(
        "Created session %s personas=%s primary_room=%s rooms_emitted=%d",
        session_id,
        persona_names,
        primary_room_name,
        len(rooms),
    )

    return CreateSessionResponse(
        session_id=session_id,
        livekit_url=settings.LIVEKIT_URL,
        video_url=request.video_url,
        rooms=rooms,
        personas=build_persona_manifest(),
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
