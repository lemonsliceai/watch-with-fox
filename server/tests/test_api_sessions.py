"""Contract tests for ``POST /api/sessions``.

These pin the response shape and the dispatch metadata schema. The
extension consumes the response, the agent worker consumes the
dispatch metadata, and both ship together — so a drift in either
shape is a breaking change we want a unit test to catch before
deploy.

Notes:
* JWTs are decoded via the same ``jwt`` library LiveKit uses
  internally (``livekit.api.access_token`` calls ``jwt.encode``), so
  the decoder and the encoder agree on claim layout.
* The tests are hermetic — the LiveKit REST API is never called (the
  server only mints local JWTs for ``/api/sessions``), and the
  database boundary is mocked via ``create_session``.
"""

from __future__ import annotations

import json

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from podcast_commentary.api import livekit_tokens
from podcast_commentary.api.routes import personas as personas_module
from podcast_commentary.api.routes import sessions as sessions_module
from podcast_commentary.core import config as core_config

from tests.agent._stub_config import make_stub_config


# First entry is primary (route uses PERSONAS ordering as the source
# of truth — no separate PRIMARY_PERSONA setting).
PERSONA_NAMES = ["persona_a", "persona_b"]
PRIMARY_PERSONA = PERSONA_NAMES[0]
SECONDARY_PERSONA = PERSONA_NAMES[1]


def _install_test_settings(monkeypatch) -> None:
    """Wire test-only settings onto the live ``settings`` singleton."""
    monkeypatch.setattr(core_config.settings, "LIVEKIT_API_KEY", "test-api-key")
    monkeypatch.setattr(core_config.settings, "LIVEKIT_API_SECRET", "test-api-secret")
    monkeypatch.setattr(core_config.settings, "LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setattr(core_config.settings, "AGENT_NAME", "test-agent")
    monkeypatch.setattr(core_config.settings, "PERSONAS", ",".join(PERSONA_NAMES))
    # Token-minting helper reads through its own ``settings`` reference.
    monkeypatch.setattr(livekit_tokens.settings, "LIVEKIT_API_KEY", "test-api-key")
    monkeypatch.setattr(livekit_tokens.settings, "LIVEKIT_API_SECRET", "test-api-secret")


@pytest.fixture
def client(monkeypatch):
    """Hermetic FastAPI client: real route, mocked DB + LiveKit creds.

    We deliberately skip ``app.py``'s lifespan (warm_pool / migrations)
    by mounting the router on a fresh ``FastAPI`` instance — the route
    under test only needs the LiveKit credentials and a stubbed
    ``create_session``.
    """
    _install_test_settings(monkeypatch)

    captured: dict[str, object] = {}

    async def _fake_create_session(
        room_name: str,
        video_url: str,
        video_title: str | None = None,
        rooms: dict[str, str] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        anonymous_id: str | None = None,
    ) -> str:
        captured["room_name"] = room_name
        captured["video_url"] = video_url
        captured["video_title"] = video_title
        captured["rooms"] = rooms
        captured["session_id"] = session_id
        captured["user_id"] = user_id
        captured["anonymous_id"] = anonymous_id
        return session_id or "stub-session-id"

    monkeypatch.setattr(sessions_module, "create_session", _fake_create_session)

    # Route loads each persona's FoxConfig to populate the dispatch metadata
    # (label, avatar_url). Stub the loader so the test never touches the
    # real preset bank — keeps it character-agnostic and decoupled from
    # whichever presets happen to ship today.
    monkeypatch.setattr(sessions_module, "load_config", make_stub_config)
    # build_persona_manifest (called by the sessions route via the
    # imported helper) resolves persona names through its own module's
    # ``load_config`` reference — stub that too so the test stays
    # hermetic against the real preset bank.
    monkeypatch.setattr(personas_module, "load_config", make_stub_config)

    app = FastAPI()
    app.include_router(sessions_module.router)
    test_client = TestClient(app)
    test_client.captured_db_args = captured  # type: ignore[attr-defined]
    return test_client


def _decode(token: str) -> dict:
    """Decode a JWT minted by livekit.api — signature check skipped (not the SUT)."""
    return jwt.decode(token, options={"verify_signature": False})


def _post_session(client: TestClient, **overrides) -> dict:
    payload = {"video_url": "https://example.com/video", "video_title": "Episode 1"}
    payload.update(overrides)
    response = client.post("/api/sessions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Response shape — snapshot of the public contract
# ---------------------------------------------------------------------------


def test_response_shape_matches_spec(client):
    body = _post_session(client)

    # Top-level keys are exactly the public contract — no more, no less.
    # Locking the set protects extension consumers from silent additions
    # they don't know how to ignore.
    assert set(body.keys()) == {
        "session_id",
        "livekit_url",
        "video_url",
        "rooms",
        "personas",
    }
    assert isinstance(body["session_id"], str) and body["session_id"]
    assert body["livekit_url"] == "wss://test.livekit.cloud"
    assert body["video_url"] == "https://example.com/video"
    assert isinstance(body["rooms"], list)
    assert len(body["rooms"]) == len(PERSONA_NAMES)

    # Persona manifest mirrors PERSONAS order — the extension's avatar
    # stack renders directly from this, so order + role are part of the
    # public contract.
    assert isinstance(body["personas"], list)
    assert [p["name"] for p in body["personas"]] == PERSONA_NAMES
    expected_persona_keys = {"name", "label", "descriptor", "preview_filename", "role"}
    for entry in body["personas"]:
        assert set(entry.keys()) == expected_persona_keys
    assert body["personas"][0]["role"] == "primary"
    for entry in body["personas"][1:]:
        assert entry["role"] == "secondary"

    # Each RoomEntry is a flat record of (persona, room_name, token, role).
    expected_entry_keys = {"persona", "room_name", "token", "role"}
    for entry in body["rooms"]:
        assert set(entry.keys()) == expected_entry_keys
        assert isinstance(entry["persona"], str) and entry["persona"]
        assert isinstance(entry["room_name"], str) and entry["room_name"]
        assert isinstance(entry["token"], str) and entry["token"]
        assert entry["role"] in {"primary", "secondary"}

    # Personas in the response cover the configured set, no extras, no dupes.
    personas_in_response = [e["persona"] for e in body["rooms"]]
    assert sorted(personas_in_response) == sorted(PERSONA_NAMES)
    assert len(personas_in_response) == len(set(personas_in_response))

    # Per-persona room name follows the deterministic pattern.
    session_id = body["session_id"]
    for entry in body["rooms"]:
        assert entry["room_name"] == f"{session_id}-{entry['persona']}"


def test_exactly_one_primary_entry(client):
    body = _post_session(client)

    primaries = [e for e in body["rooms"] if e["role"] == "primary"]
    secondaries = [e for e in body["rooms"] if e["role"] == "secondary"]

    assert len(primaries) == 1, f"expected exactly one primary, got {len(primaries)}"
    assert primaries[0]["persona"] == PRIMARY_PERSONA
    # Primary + secondaries account for every entry — nothing else slipped in.
    assert len(primaries) + len(secondaries) == len(body["rooms"])


# ---------------------------------------------------------------------------
# Token contract — primary vs. secondary grants
# ---------------------------------------------------------------------------


def test_primary_token_grants_do_not_set_agent_true(client):
    """The primary token is for the *extension* (the human user), not the
    agent worker. Setting ``agent: true`` here would let the extension
    register as an agent participant, which is wrong end-to-end."""
    body = _post_session(client)

    primary = next(e for e in body["rooms"] if e["role"] == "primary")
    claims = _decode(primary["token"])
    video = claims["video"]

    # ``video.agent`` may be omitted (current behaviour) or explicitly
    # falsy. It must NEVER be truthy.
    assert not video.get("agent"), f"primary token must not have agent=true; got video={video!r}"
    # Sanity: it's still a participant join token.
    assert video["roomJoin"] is True
    assert video["room"] == primary["room_name"]
    # The extension's primary participant doesn't get the agent ``kind``.
    assert claims.get("kind") != "agent"


def test_secondary_agent_tokens_have_agent_true_and_correct_room(client):
    """Each secondary room's ``agent_token`` (in the dispatch metadata) is
    the JWT the agent worker uses to self-join that secondary room.
    It MUST carry ``agent: true`` and be scoped to the matching room."""
    body = _post_session(client)

    metadata = _extract_dispatch_metadata(body)
    secondary_rooms = metadata["secondary_rooms"]

    # Every non-primary persona has exactly one secondary_rooms entry.
    expected_personas = {p for p in PERSONA_NAMES if p != PRIMARY_PERSONA}
    actual_personas = {entry["persona"] for entry in secondary_rooms}
    assert actual_personas == expected_personas

    response_room_by_persona = {e["persona"]: e["room_name"] for e in body["rooms"]}

    for entry in secondary_rooms:
        persona = entry["persona"]
        agent_token = entry["agent_token"]
        claims = _decode(agent_token)
        video = claims["video"]

        # ``agent: true`` is required. Without it the worker registers
        # as a standard participant and dispatch correlation breaks.
        assert video.get("agent") is True, (
            f"secondary agent_token for {persona!r} missing agent=true: {video!r}"
        )
        # Room claim must match the room name in the same metadata entry
        # AND the room name in the top-level response — the three sources
        # of truth must agree.
        assert video["room"] == entry["room_name"]
        assert video["room"] == response_room_by_persona[persona]
        # Agent identity is deterministic per (persona, session).
        assert claims["sub"] == f"agent-{persona}-{body['session_id']}"
        assert claims["kind"] == "agent"


# ---------------------------------------------------------------------------
# Dispatch metadata — primary room only
# ---------------------------------------------------------------------------


def test_dispatch_metadata_present_only_on_primary_token(client):
    """``RoomAgentDispatch`` is what triggers the agent worker. It must
    live on exactly one token (the primary), or LiveKit will dispatch
    multiple workers per session."""
    body = _post_session(client)

    primary_with_dispatch = 0
    secondary_with_dispatch = 0
    for entry in body["rooms"]:
        claims = _decode(entry["token"])
        room_config = claims.get("roomConfig") or {}
        agents = room_config.get("agents") or []
        if entry["role"] == "primary":
            if agents:
                primary_with_dispatch += 1
        else:
            if agents:
                secondary_with_dispatch += 1

    assert primary_with_dispatch == 1, "primary token must carry exactly one RoomAgentDispatch"
    assert secondary_with_dispatch == 0, (
        "secondary tokens must NOT carry a RoomAgentDispatch — "
        "that would dispatch a duplicate agent worker"
    )


def test_dispatch_metadata_payload_matches_schema(client):
    """The metadata blob is the agent worker's source of truth for
    spinning up secondary rooms. Lock the schema here so a server-side
    drift surfaces as a unit-test failure, not a silent runtime bug in
    the agent."""
    body = _post_session(client)

    # roomConfig payload itself is well-formed.
    primary = next(e for e in body["rooms"] if e["role"] == "primary")
    primary_claims = _decode(primary["token"])
    agents = primary_claims["roomConfig"]["agents"]
    assert len(agents) == 1
    assert agents[0]["agentName"] == "test-agent"

    # Embedded JSON parses into the dispatch metadata schema.
    metadata = json.loads(agents[0]["metadata"])
    assert set(metadata.keys()) == {
        "session_id",
        "video_url",
        "video_title",
        "primary_persona",
        "all_personas",
        "secondary_rooms",
        "personas",
    }
    assert metadata["session_id"] == body["session_id"]
    assert metadata["video_url"] == "https://example.com/video"
    assert metadata["video_title"] == "Episode 1"
    assert metadata["primary_persona"] == PRIMARY_PERSONA
    assert metadata["all_personas"] == PERSONA_NAMES

    # secondary_rooms entries match the dispatch metadata schema exactly.
    for entry in metadata["secondary_rooms"]:
        assert set(entry.keys()) == {"persona", "room_name", "agent_token"}
        assert entry["persona"] != PRIMARY_PERSONA  # primary not duplicated here

    # personas descriptors match the dispatch metadata schema exactly.
    assert {p["name"] for p in metadata["personas"]} == set(PERSONA_NAMES)
    for p in metadata["personas"]:
        assert set(p.keys()) == {"name", "label", "avatar_url"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_dispatch_metadata(body: dict) -> dict:
    """Pull the dispatch metadata JSON out of the primary token's roomConfig."""
    primary = next(e for e in body["rooms"] if e["role"] == "primary")
    claims = _decode(primary["token"])
    agents = claims["roomConfig"]["agents"]
    assert len(agents) == 1, f"expected one dispatch entry, got {len(agents)}"
    return json.loads(agents[0]["metadata"])
