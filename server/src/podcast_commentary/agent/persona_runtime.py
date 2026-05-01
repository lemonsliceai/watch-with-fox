"""PersonaRuntimeBuilder — wires one ``AgentSession`` + avatar per persona.

The agent ``entrypoint`` (see ``main.py``) parses dispatch metadata then
needs to, for every persona:

  * resolve the right ``rtc.Room`` (primary or self-joined secondary)
  * build an ``AgentSession`` from the persona's ``FoxConfig``
  * start the LemonSlice avatar under a unique participant identity
  * call ``session.start`` with the matching audio-output options
  * record the per-persona context the Director consumes

This module owns that loop so the entrypoint stays a thin
metadata-parser + Director-launcher. The builder is stateful only
across one entrypoint invocation: the runtimes it accumulates are
returned and then handed off to the Director, which becomes the long-
lived owner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from livekit import rtc
from livekit.agents import AgentSession, room_io

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.director import PersonaContext
from podcast_commentary.agent.dispatch_metadata import DispatchMetadata
from podcast_commentary.agent.fox_config import FoxConfig, load_config
from podcast_commentary.agent.secondary_room import SecondaryRoomConnector

logger = logging.getLogger("podcast-commentary.agent")


# Stable label values for ``room_role``. ``primary`` is the room the
# RoomAgentDispatch landed on (``ctx.room``); each non-primary persona
# lives in a "secondary" room joined via ``SecondaryRoomConnector``.
ROOM_ROLE_PRIMARY = "primary"
ROOM_ROLE_SECONDARY = "secondary"

# Track-name prefix the extension uses to peel a persona off a direct-publish
# audio track. Each audio-only persona publishes under ``persona-<name>``
# so the side-panel can route every persona's track separately even though
# they share one ``local_participant``.
_PERSONA_TRACK_PREFIX = "persona-"


def avatar_identity_for(persona_name: str) -> str:
    """Per-persona avatar participant identity.

    Each LemonSlice instance must publish under a unique identity or
    LiveKit treats them as the same participant and only one set of
    tracks survives. The Chrome extension routes incoming tracks by
    matching this prefix — see ``sidepanel.js``.
    """
    return f"lemonslice-avatar-{persona_name}"


def persona_track_name(persona_name: str) -> str:
    return f"{_PERSONA_TRACK_PREFIX}{persona_name}"


@dataclass
class PersonaRuntime:
    """Diagnostic snapshot of one persona's per-job runtime."""

    persona: PersonaAgent
    session: AgentSession
    room: rtc.Room
    is_primary: bool
    avatar_session_id: str | None
    avatar_identity: str | None


@dataclass
class BuiltRuntimes:
    """Result of building every persona's runtime in dispatch order."""

    contexts: list[PersonaContext]
    runtimes: list[PersonaRuntime]
    avatar_identities: dict[str, str]


# Type alias for the avatar starter callback: kept narrow so this module
# doesn't pull in the LemonSlice plugin or metrics plumbing — main.py
# owns those concerns.
StartAvatar = Callable[..., Awaitable[str | None]]


class PersonaRuntimeBuilder:
    """Iterate ``meta.all_personas`` and build one runtime per persona.

    Side effects per persona:
      * ``start_avatar(...)`` — may publish an avatar into the persona's room
      * ``session.start(...)`` — attaches the agent to the room

    The builder DOES NOT own room lifecycles: secondary rooms are
    connected by the caller before this runs. On any unrecoverable
    persona failure (e.g. an ``all_personas`` entry with no room) the
    builder returns ``None`` and the caller is responsible for tearing
    down already-built sessions.
    """

    def __init__(
        self,
        *,
        meta: DispatchMetadata,
        primary_room: rtc.Room,
        connector_by_persona: dict[str, SecondaryRoomConnector],
        vad: Any,
        build_session: Callable[[FoxConfig, Any], AgentSession],
        start_avatar: StartAvatar,
        avatar_startup_ms: dict[str, float],
    ) -> None:
        self._meta = meta
        self._primary_room = primary_room
        self._connector_by_persona = connector_by_persona
        self._descriptor_by_name = {p.name: p for p in meta.personas}
        self._vad = vad
        self._build_session = build_session
        self._start_avatar = start_avatar
        self._avatar_startup_ms = avatar_startup_ms

    async def build(self) -> BuiltRuntimes | None:
        contexts: list[PersonaContext] = []
        runtimes: list[PersonaRuntime] = []
        avatar_identities: dict[str, str] = {}

        for persona_name in self._meta.all_personas:
            built = await self._build_one(persona_name)
            if built is None:
                return None
            contexts.append(
                PersonaContext(
                    persona=built.persona,
                    room=built.room,
                    session=built.session,
                )
            )
            runtimes.append(built)
            if built.avatar_identity is not None:
                avatar_identities[persona_name] = built.avatar_identity

        return BuiltRuntimes(
            contexts=contexts,
            runtimes=runtimes,
            avatar_identities=avatar_identities,
        )

    async def _build_one(self, persona_name: str) -> PersonaRuntime | None:
        is_primary = persona_name == self._meta.primary_persona
        if is_primary:
            persona_room: rtc.Room = self._primary_room
        else:
            connector = self._connector_by_persona.get(persona_name)
            if connector is None:
                # all_personas listed a non-primary persona that has no
                # secondary_rooms entry. The Pydantic model already enforces
                # the inverse (no duplicates, primary not in secondaries),
                # but this catches a stray all_personas drift.
                logger.error(
                    "[%s] Persona has no primary or secondary room — aborting job",
                    persona_name,
                )
                return None
            persona_room = connector.room

        descriptor = self._descriptor_by_name.get(persona_name)
        avatar_url = descriptor.avatar_url if descriptor else None
        config = load_config(persona_name)

        logger.info(
            "[%s] === SYSTEM PROMPT ===\n%s\n=== END SYSTEM PROMPT ===",
            persona_name,
            config.persona.system_prompt,
        )

        session = self._build_session(config, self._vad)
        identity = avatar_identity_for(persona_name)
        avatar_session_id = await self._start_avatar(
            config=config,
            avatar_url=avatar_url,
            session=session,
            room=persona_room,
            identity=identity,
            room_role=ROOM_ROLE_PRIMARY if is_primary else ROOM_ROLE_SECONDARY,
            avatar_startup_ms=self._avatar_startup_ms,
        )

        persona = PersonaAgent(config=config, session_id=self._meta.session_id)

        # Each persona owns its room outright, so a name collision between
        # persona audio tracks is impossible — but we keep the per-persona
        # track name anyway so the extension's routing logic stays uniform.
        has_avatar = avatar_session_id is not None
        if has_avatar:
            audio_output: room_io.AudioOutputOptions | bool = False
        else:
            audio_output = room_io.AudioOutputOptions(
                track_name=persona_track_name(persona_name),
            )
        await session.start(
            agent=persona,
            room=persona_room,
            room_options=room_io.RoomOptions(
                audio_input=False,
                audio_output=audio_output,
                close_on_disconnect=False,
            ),
        )

        logger.info(
            "[%s] AgentSession started in room=%s (primary=%s avatar=%s)",
            persona_name,
            getattr(persona_room, "name", "?"),
            is_primary,
            has_avatar,
        )
        return PersonaRuntime(
            persona=persona,
            session=session,
            room=persona_room,
            is_primary=is_primary,
            avatar_session_id=avatar_session_id,
            avatar_identity=identity if has_avatar else None,
        )
