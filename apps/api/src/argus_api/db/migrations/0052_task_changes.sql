-- Latest diff snapshot of a task attempt's workspace (docs/specs/task-changes.spec.md).
-- One row per attempt, replaced on each write; the current attempt and the two
-- before it keep theirs. JSON columns are text.
CREATE TABLE task_changes (
    task_id     TEXT NOT NULL REFERENCES tasks (id),
    attempt     INTEGER NOT NULL,
    at          TEXT NOT NULL,
    received_at TEXT NOT NULL,
    branch      TEXT NOT NULL,
    base_ref    TEXT,
    base_sha    TEXT,
    head        TEXT,
    commits     TEXT NOT NULL,
    files       TEXT NOT NULL,
    untracked   TEXT NOT NULL,
    patch       TEXT NOT NULL,
    truncated   TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt)
) STRICT, WITHOUT ROWID;
