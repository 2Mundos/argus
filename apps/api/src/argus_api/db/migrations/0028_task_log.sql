-- Live log of an attempt's output (docs/specs/task-live-log.spec.md).
-- `seq` counts lines per task across attempts and never repeats (`tasks.log_seq`);
-- each attempt keeps a bounded ring, trimmed on write.
ALTER TABLE tasks ADD COLUMN output_at TEXT;
ALTER TABLE tasks ADD COLUMN log_seq INTEGER NOT NULL DEFAULT 0;

CREATE TABLE task_log (
    task_id TEXT NOT NULL REFERENCES tasks (id),
    attempt INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    source  TEXT NOT NULL CHECK (source IN ('worker', 'delegate')),
    at      TEXT NOT NULL,
    line    TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt, seq)
) STRICT, WITHOUT ROWID;
