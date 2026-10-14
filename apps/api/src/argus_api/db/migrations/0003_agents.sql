CREATE TABLE agents (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('orchestrator', 'worker')),
    instance_id       TEXT,
    capabilities      TEXT NOT NULL CHECK (json_valid(capabilities)),
    state             TEXT NOT NULL CHECK (state IN ('active', 'draining', 'gone', 'lost')),
    last_heartbeat_at TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    ended_at          TEXT,
    end_reason        TEXT,
    CHECK ((state IN ('gone', 'lost')) = (ended_at IS NOT NULL))
) STRICT;

-- The reaper scans live agents by heartbeat age.
CREATE INDEX agents_live ON agents (last_heartbeat_at) WHERE state IN ('active', 'draining');
CREATE INDEX agents_instance ON agents (instance_id);

-- Bearer tokens, stored as SHA-256 of a high-entropy secret.
-- bootstrap: one-time, minted for an instance, redeemed by POST /agents/register.
-- agent: long-lived, bound to one agent, revoked when it ends.
CREATE TABLE tokens (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('bootstrap', 'agent')),
    role        TEXT NOT NULL CHECK (role IN ('orchestrator', 'worker')),
    agent_id    TEXT REFERENCES agents (id),
    instance_id TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    used_at     TEXT,
    revoked_at  TEXT,
    CHECK ((kind = 'agent') = (agent_id IS NOT NULL))
) STRICT;

CREATE INDEX tokens_agent ON tokens (agent_id) WHERE revoked_at IS NULL;
CREATE INDEX tokens_instance ON tokens (instance_id) WHERE revoked_at IS NULL;
