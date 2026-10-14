-- Pools of interchangeable workers and the instances (containers) that serve
-- them. Desired state lives on the pool; the reconciler converges instances to it
-- (worker lifecycle 3.5).
CREATE TABLE pools (
    name                TEXT PRIMARY KEY,
    target              TEXT NOT NULL CHECK (target IN ('docker', 'kubernetes')),
    runtime             TEXT NOT NULL CHECK (runtime IN ('plain', 'nanoclaw')),
    image               TEXT NOT NULL,
    capabilities        TEXT NOT NULL CHECK (json_valid(capabilities)),
    agents_per_instance INTEGER NOT NULL DEFAULT 1 CHECK (agents_per_instance >= 1),
    desired_replicas    INTEGER NOT NULL DEFAULT 0 CHECK (desired_replicas >= 0),
    max_replicas        INTEGER NOT NULL CHECK (max_replicas >= 0),
    config              TEXT NOT NULL CHECK (json_valid(config)),
    status              TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'unreachable', 'failing')),
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK (desired_replicas <= max_replicas)
) STRICT;

-- reserved:    capacity slot taken; container not confirmed yet (start pending
--              or ambiguous; resolved by provider lookup, never by a second start).
-- running:     the provider has the container.
-- terminating: stop requested; the slot stays taken until the provider confirms.
-- terminated:  the provider confirmed it stopped or no longer exists.
CREATE TABLE instances (
    id             TEXT PRIMARY KEY,
    pool           TEXT NOT NULL REFERENCES pools (name),
    provider_ref   TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('reserved', 'running', 'terminating', 'terminated')),
    draining       INTEGER NOT NULL DEFAULT 0 CHECK (draining IN (0, 1)),
    start_attempts INTEGER NOT NULL DEFAULT 0,
    last_start_at  TEXT,
    last_error     TEXT,
    exit_reason    TEXT,
    created_at     TEXT NOT NULL,
    running_at     TEXT,
    terminating_at TEXT,
    terminated_at  TEXT,
    CHECK ((state = 'terminated') = (terminated_at IS NOT NULL))
) STRICT;

CREATE INDEX instances_live ON instances (pool, state) WHERE state != 'terminated';

-- Structured context for decisions (e.g. the replica count a policy gate holds).
ALTER TABLE decisions ADD COLUMN payload TEXT CHECK (payload IS NULL OR json_valid(payload));
