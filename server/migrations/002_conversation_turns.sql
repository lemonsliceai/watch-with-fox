-- Per-session conversation history: every podcast utterance, every Fox
-- line, every user push-to-talk turn gets one row. This is the durable
-- transcript of each viewing session and what feeds the agent's working state.

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

-- Running LLM summary persisted per session so we can inspect or resume it
-- across restarts.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary TEXT;
