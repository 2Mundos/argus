CREATE TABLE tasks (
    id                    TEXT PRIMARY KEY,
    parent_id             TEXT REFERENCES tasks (id),
    kind                  TEXT NOT NULL,
    title                 TEXT NOT NULL,
    spec                  TEXT NOT NULL,
    acceptance            TEXT NOT NULL CHECK (json_valid(acceptance)),
    input                 TEXT NOT NULL CHECK (json_valid(input)),
    required_capabilities TEXT NOT NULL CHECK (json_valid(required_capabilities)),
    priority              INTEGER NOT NULL,
    state                 TEXT NOT NULL CHECK (state IN (
        'awaiting_approval', 'pending', 'ready', 'claimed', 'blocked',
        'in_review', 'succeeded', 'failed', 'cancelled')),
    requires_approval     INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
    review                TEXT NOT NULL CHECK (review IN ('none', 'human', 'orchestrator')),
    assignee_agent_id     TEXT,
    lease_expires_at      TEXT,
    lease_ttl_s           INTEGER NOT NULL,
    -- attempt: bumped on every claim, never decreases; fences stale writers.
    -- failures: counts fail/expire only; compared against max_attempts.
    attempt               INTEGER NOT NULL DEFAULT 0,
    failures              INTEGER NOT NULL DEFAULT 0,
    max_attempts          INTEGER NOT NULL,
    result                TEXT CHECK (result IS NULL OR json_valid(result)),
    error                 TEXT,
    block_reason          TEXT,
    created_by            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    -- An assignee and a lease exist exactly while the task is claimed.
    CHECK ((state = 'claimed') = (assignee_agent_id IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK ((state = 'claimed') OR (assignee_agent_id IS NULL AND lease_expires_at IS NULL))
) STRICT;

CREATE INDEX tasks_ready ON tasks (priority DESC, created_at, id) WHERE state = 'ready';
CREATE INDEX tasks_lease ON tasks (lease_expires_at) WHERE state = 'claimed';
CREATE INDEX tasks_state ON tasks (state, created_at);
CREATE INDEX tasks_parent ON tasks (parent_id);

CREATE TABLE task_deps (
    task_id       TEXT NOT NULL REFERENCES tasks (id),
    depends_on_id TEXT NOT NULL REFERENCES tasks (id),
    PRIMARY KEY (task_id, depends_on_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX task_deps_reverse ON task_deps (depends_on_id, task_id);
