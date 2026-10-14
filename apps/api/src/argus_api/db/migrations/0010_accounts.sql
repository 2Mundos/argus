-- Provider accounts. The registry lives here; credentials never do: secret_ref
-- points into the SOPS file (worker lifecycle 3.9).
CREATE TABLE accounts (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    provider        TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('api_key', 'subscription')),
    secret_ref      TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    max_leases      INTEGER CHECK (max_leases IS NULL OR max_leases > 0),
    state           TEXT NOT NULL CHECK (state IN ('available', 'exhausted', 'disabled')),
    exhausted_until TEXT,
    disabled_reason TEXT,
    -- 1 when Argus disabled it (e.g. missing secret) and may re-enable it itself.
    auto_disabled   INTEGER NOT NULL DEFAULT 0 CHECK (auto_disabled IN (0, 1)),
    labels          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(labels)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK ((state = 'exhausted') = (exhausted_until IS NOT NULL)),
    CHECK ((state = 'disabled') = (disabled_reason IS NOT NULL))
) STRICT;

CREATE TABLE account_leases (
    id             TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES accounts (id),
    agent_id       TEXT NOT NULL REFERENCES agents (id),
    providers      TEXT NOT NULL CHECK (json_valid(providers)),
    leased_at      TEXT NOT NULL,
    released_at    TEXT,
    release_reason TEXT
) STRICT;

-- One active lease per agent; leases end with the agent (lost/gone) or on release.
CREATE UNIQUE INDEX account_leases_active_agent ON account_leases (agent_id)
    WHERE released_at IS NULL;
CREATE INDEX account_leases_active_account ON account_leases (account_id)
    WHERE released_at IS NULL;

-- Reported usage. Recorded, never enforced.
CREATE TABLE account_usage (
    id            INTEGER PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts (id),
    agent_id      TEXT,
    task_id       TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    at            TEXT NOT NULL
) STRICT;

CREATE INDEX account_usage_account ON account_usage (account_id, at);
