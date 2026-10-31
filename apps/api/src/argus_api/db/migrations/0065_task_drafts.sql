-- Task drafts (docs/specs/task-drafts.spec.md): definitions that are not tasks, so
-- nothing claims, lists or counts them, until a publish creates the tasks.
-- `updated_at` is the draft's version. Additive and IF NOT EXISTS: idempotent.
CREATE TABLE IF NOT EXISTS task_drafts (
    id                TEXT PRIMARY KEY,
    body              TEXT NOT NULL CHECK (json_valid(body)),
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_by        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    published_at      TEXT,
    published_task_id TEXT REFERENCES tasks (id),
    publication_key   TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS task_drafts_open ON task_drafts (created_at, id)
    WHERE published_at IS NULL;

-- One row per successful publish: a retry with the same key and request gets the
-- stored answer back instead of publishing again (3.4).
CREATE TABLE IF NOT EXISTS task_draft_publications (
    idempotency_key TEXT PRIMARY KEY,
    request_digest  TEXT NOT NULL,
    answer          TEXT NOT NULL CHECK (json_valid(answer)),
    actor           TEXT NOT NULL,
    created_at      TEXT NOT NULL
) STRICT;
