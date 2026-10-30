-- Internal orchestrator: conversations/messages, system pool marker, task origin,
-- and per-turn usage (docs/specs/internal-orchestrator.spec.md).

CREATE TABLE conversations (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('chat', 'updates')),
    title         TEXT NOT NULL,
    title_source  TEXT NOT NULL CHECK (title_source IN ('orchestrator', 'admin')),
    created_by    TEXT NOT NULL,
    archived_at   TEXT,
    session_ref   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX conversations_one_updates
    ON conversations (kind) WHERE kind = 'updates';

CREATE INDEX conversations_updated
    ON conversations (archived_at, updated_at DESC);

CREATE TABLE messages (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL REFERENCES conversations (id),
    author           TEXT NOT NULL,
    body             TEXT NOT NULL,
    state            TEXT NOT NULL
                     CHECK (state IN ('queued', 'delivered', 'answered', 'posted')),
    truncated        INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    turn_id          TEXT,
    created_at       TEXT NOT NULL,
    delivered_at     TEXT
) STRICT;

CREATE INDEX messages_conversation_created
    ON messages (conversation_id, created_at, id);
CREATE INDEX messages_queued
    ON messages (conversation_id, state, created_at)
    WHERE state = 'queued';

ALTER TABLE pools ADD COLUMN system TEXT
    CHECK (system IS NULL OR system = 'orchestrator');

CREATE UNIQUE INDEX pools_system_unique ON pools (system) WHERE system IS NOT NULL;

ALTER TABLE tasks ADD COLUMN origin_conversation_id TEXT
    REFERENCES conversations (id);

CREATE INDEX tasks_origin_conversation
    ON tasks (origin_conversation_id)
    WHERE origin_conversation_id IS NOT NULL;

CREATE TABLE conversation_usage (
    id               INTEGER PRIMARY KEY,
    conversation_id  TEXT NOT NULL REFERENCES conversations (id),
    turn_id          TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cost_usd         REAL,
    model            TEXT,
    at               TEXT NOT NULL,
    measurement      TEXT NOT NULL DEFAULT 'measured'
                     CHECK (measurement IN ('measured', 'unpriced', 'unknown'))
) STRICT;

CREATE INDEX conversation_usage_conversation
    ON conversation_usage (conversation_id, at);
CREATE INDEX conversation_usage_at ON conversation_usage (at);
