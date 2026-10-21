-- The definition each attempt was handed at claim (docs/specs/task-edit.spec.md 3.5).
-- Append-only: an edit made after a claim never rewrites what that attempt ran.
-- `attempt` restarts at 1 after a retry, so rows are ordered by `id`.
CREATE TABLE task_attempts (
    id         INTEGER PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks (id),
    attempt    INTEGER NOT NULL,
    agent_id   TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    definition TEXT NOT NULL CHECK (json_valid(definition))
) STRICT;

CREATE INDEX task_attempts_task ON task_attempts (task_id, id);
