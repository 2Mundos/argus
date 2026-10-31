-- Routines: recurring work on a cron schedule (docs/specs/routines.spec.md 3.1).
-- Run tasks point back with `created_by = 'routine:<id>'`, not a foreign key, so
-- deleting a routine keeps them; `last_task_id` is cleared if its task goes.
CREATE TABLE routines (
    id           TEXT PRIMARY KEY,
    project      TEXT NOT NULL REFERENCES projects (slug),
    slug         TEXT NOT NULL,
    title        TEXT NOT NULL,
    schedule     TEXT NOT NULL,
    timezone     TEXT NOT NULL DEFAULT 'UTC',
    template     TEXT NOT NULL CHECK (json_valid(template)),
    findings     TEXT NOT NULL CHECK (findings IN ('off', 'approval', 'auto')),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    next_run_at  TEXT,
    last_run_at  TEXT,
    last_task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (project, slug)
) STRICT;

-- The scheduler's due scan: enabled routines by next run.
CREATE INDEX routines_due ON routines (enabled, next_run_at);
