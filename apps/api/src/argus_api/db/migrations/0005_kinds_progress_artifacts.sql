CREATE TABLE task_kinds (
    name                 TEXT PRIMARY KEY,
    description          TEXT NOT NULL,
    input_schema         TEXT CHECK (input_schema IS NULL OR json_valid(input_schema)),
    result_schema        TEXT CHECK (result_schema IS NULL OR json_valid(result_schema)),
    default_capabilities TEXT CHECK (default_capabilities IS NULL OR json_valid(default_capabilities)),
    default_lease_ttl_s  INTEGER,
    default_max_attempts INTEGER,
    default_review       TEXT CHECK (default_review IN ('none', 'human', 'orchestrator')),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
) STRICT;

INSERT INTO task_kinds (name, description, created_at, updated_at) VALUES (
    'generic', 'Free-form work: no input or result schema.',
    '2026-10-14T00:00:00.000000Z', '2026-10-14T00:00:00.000000Z');

-- Latest progress note, denormalized for the dashboard; history lives in events.
ALTER TABLE tasks ADD COLUMN progress_message TEXT;
ALTER TABLE tasks ADD COLUMN progress_percent INTEGER CHECK (progress_percent BETWEEN 0 AND 100);
ALTER TABLE tasks ADD COLUMN progress_at TEXT;

CREATE TABLE artifacts (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks (id),
    attempt    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, attempt, name)
) STRICT;
