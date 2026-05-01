-- Schema for podcast commentary sessions.
--
-- You do not need to run this by hand. The FastAPI lifespan hook calls
-- `ensure_schema()` in `src/podcast_commentary/core/db.py`, which issues
-- the same idempotent DDL on startup against `DATABASE_URL`. This file
-- exists as a reference and as a one-shot script for setups that prefer
-- to provision the schema out-of-band (e.g. `psql $DATABASE_URL -f 001_init.sql`).

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
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_created
    ON sessions(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_anonymous_created
    ON sessions(anonymous_id, created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,             -- 'podcast' | 'agent' | 'user' | 'system'
    content TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
    ON conversation_messages(session_id, created_at);
