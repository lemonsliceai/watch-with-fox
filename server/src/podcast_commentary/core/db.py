import json
import logging
from typing import Any

import asyncpg

from podcast_commentary.core.config import settings

logger = logging.getLogger("podcast-commentary.db")

_pool: asyncpg.Pool | None = None
_pool_unavailable_warned: bool = False


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not settings.DATABASE_URL:
            raise RuntimeError("DATABASE_URL not set")
        _pool = await asyncpg.create_pool(
            settings.DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=15,
        )
    return _pool


async def _try_get_pool() -> asyncpg.Pool | None:
    """Return the pool if DATABASE_URL is configured, else log once and return None.

    Agent-side logging paths use this so that a missing DB doesn't crash the
    session — we just lose persistence for that run.
    """
    global _pool_unavailable_warned
    if not settings.DATABASE_URL:
        if not _pool_unavailable_warned:
            logger.warning("DATABASE_URL not set — conversation persistence disabled")
            _pool_unavailable_warned = True
        return None
    try:
        return await _get_pool()
    except Exception:
        if not _pool_unavailable_warned:
            logger.warning(
                "Failed to open DB pool — conversation persistence disabled", exc_info=True
            )
            _pool_unavailable_warned = True
        return None


async def warm_pool() -> None:
    if not settings.DATABASE_URL:
        logger.warning("DATABASE_URL not set — skipping DB pool warm-up")
        return
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    logger.info("Database pool warmed")


async def ensure_schema() -> None:
    """Create the schema on a fresh database. No-op once tables exist.

    Pre-launch, single-tenant, no users to migrate — so we ship one
    canonical schema rather than a migration ladder. If the schema
    needs to evolve post-launch, switch to a real migration tool
    (alembic) rather than re-introducing ad-hoc ALTER calls here.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                video_url TEXT NOT NULL,
                video_title TEXT,
                room_name TEXT NOT NULL UNIQUE,
                rooms JSONB,
                user_id TEXT,
                anonymous_id TEXT,
                status TEXT DEFAULT 'created',
                summary TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                ended_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_user_created
            ON sessions(user_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_anonymous_created
            ON sessions(anonymous_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata JSONB,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
            ON conversation_messages(session_id, created_at)
        """)
    logger.info("Schema ensured")


async def create_session(
    room_name: str,
    video_url: str,
    video_title: str | None = None,
    rooms: dict[str, str] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    anonymous_id: str | None = None,
) -> str:
    """Insert a new session row and return its id.

    Pass ``session_id`` to fix the row's primary key client-side — required
    when callers need to derive deterministic per-persona room names from
    the session id before the INSERT. Omit it to let the DB generate the
    id via ``gen_random_uuid()``.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if session_id is None:
            row = await conn.fetchrow(
                """
                INSERT INTO sessions
                    (room_name, video_url, video_title, rooms, user_id, anonymous_id)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                RETURNING id
                """,
                room_name,
                video_url,
                video_title,
                json.dumps(rooms) if rooms else None,
                user_id,
                anonymous_id,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO sessions
                    (id, room_name, video_url, video_title, rooms, user_id, anonymous_id)
                VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7)
                RETURNING id
                """,
                session_id,
                room_name,
                video_url,
                video_title,
                json.dumps(rooms) if rooms else None,
                user_id,
                anonymous_id,
            )
        return str(row["id"])


async def get_session(session_id: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
        if not row:
            return None
        result = dict(row)
        # asyncpg returns JSONB as a string; decode for callers.
        if isinstance(result.get("rooms"), str):
            result["rooms"] = json.loads(result["rooms"])
        return result


async def get_session_rooms(session_id: str) -> dict[str, str] | None:
    """Fetch the per-persona room mapping for a session.

    Returns None if the session doesn't exist or predates the dual-room
    schema (no `rooms` written at insert time).
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT rooms FROM sessions WHERE id = $1", session_id)
        if not row or row["rooms"] is None:
            return None
        rooms = row["rooms"]
        if isinstance(rooms, str):
            rooms = json.loads(rooms)
        return rooms


async def end_session(session_id: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = now() WHERE id = $1",
            session_id,
        )


async def log_conversation_message(
    session_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a single utterance/event to the conversation log.

    `role` is one of: 'podcast' (STT of the tab audio), 'agent' (a
    persona's reply), 'system' (rolling summary snapshots and lifecycle
    events). Silently skips if DATABASE_URL isn't configured so local dev
    without a DB still works.
    """
    pool = await _try_get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO conversation_messages (session_id, role, content, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                session_id,
                role,
                content,
                json.dumps(metadata) if metadata else None,
            )
    except Exception:
        logger.warning(
            "Failed to persist conversation message [role=%s, session=%s]",
            role,
            session_id,
            exc_info=True,
        )


async def update_session_summary(session_id: str, summary: str) -> None:
    """Store the latest rolling summary on the session row."""
    pool = await _try_get_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET summary = $1 WHERE id = $2",
                summary,
                session_id,
            )
    except Exception:
        logger.warning("Failed to update session summary [session=%s]", session_id, exc_info=True)
