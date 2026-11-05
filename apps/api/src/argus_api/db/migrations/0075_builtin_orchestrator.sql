-- argus: foreign keys off
-- The built-in orchestrator (docs/specs/builtin-orchestrator.spec.md 3.2): the
-- system pool may run in the Argus process (`target = 'inprocess'`,
-- `runtime = 'builtin'`). Rebuilds `pools` with the widened checks and every
-- column added since 0011; `instances` keeps referencing it by name.
CREATE TABLE pools_new (
    name                TEXT PRIMARY KEY,
    target              TEXT NOT NULL CHECK (target IN ('docker', 'kubernetes', 'inprocess')),
    runtime             TEXT NOT NULL CHECK (runtime IN ('plain', 'nanoclaw', 'builtin')),
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
    system              TEXT CHECK (system IS NULL OR system = 'orchestrator'),
    oom_streak          INTEGER NOT NULL DEFAULT 0 CHECK (oom_streak >= 0),
    CHECK (desired_replicas <= max_replicas),
    CHECK ((target = 'inprocess') = (runtime = 'builtin'))
) STRICT;
INSERT INTO pools_new
    (name, target, runtime, image, capabilities, agents_per_instance,
     desired_replicas, max_replicas, config, status, last_error, created_at,
     updated_at, system, oom_streak)
SELECT name, target, runtime, image, capabilities, agents_per_instance,
       desired_replicas, max_replicas, config, status, last_error, created_at,
       updated_at, system, oom_streak
FROM pools;
DROP TABLE pools;
ALTER TABLE pools_new RENAME TO pools;
CREATE UNIQUE INDEX pools_system_unique ON pools (system) WHERE system IS NOT NULL;
