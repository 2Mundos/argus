-- Command guard denials already recorded as task.command_blocked events
-- (docs/specs/command-guard.spec.md 3). The primary key makes a repeated report of one
-- denial (same task, attempt and tool_use id) a no-op; the row count per attempt is the
-- flood limit. The event itself lives in the events table.
CREATE TABLE task_command_blocks (
    task_id     TEXT NOT NULL REFERENCES tasks (id),
    attempt     INTEGER NOT NULL,
    tool_use_id TEXT NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt, tool_use_id)
) STRICT, WITHOUT ROWID;
