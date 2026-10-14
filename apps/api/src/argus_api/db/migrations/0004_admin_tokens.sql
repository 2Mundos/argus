-- Add admin tokens. SQLite cannot alter CHECK constraints, so rebuild the table.
CREATE TABLE tokens_new (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('bootstrap', 'agent', 'admin')),
    role        TEXT NOT NULL CHECK (role IN ('orchestrator', 'worker', 'admin')),
    label       TEXT,
    agent_id    TEXT REFERENCES agents (id),
    instance_id TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    used_at     TEXT,
    revoked_at  TEXT,
    CHECK ((kind = 'agent') = (agent_id IS NOT NULL)),
    CHECK ((kind = 'admin') = (role = 'admin'))
) STRICT;

INSERT INTO tokens_new (id, token_hash, kind, role, agent_id, instance_id,
                        created_by, created_at, expires_at, used_at, revoked_at)
SELECT id, token_hash, kind, role, agent_id, instance_id,
       created_by, created_at, expires_at, used_at, revoked_at
FROM tokens;

DROP TABLE tokens;
ALTER TABLE tokens_new RENAME TO tokens;

CREATE INDEX tokens_agent ON tokens (agent_id) WHERE revoked_at IS NULL;
CREATE INDEX tokens_instance ON tokens (instance_id) WHERE revoked_at IS NULL;
CREATE INDEX tokens_admin ON tokens (kind) WHERE kind = 'admin' AND revoked_at IS NULL;
