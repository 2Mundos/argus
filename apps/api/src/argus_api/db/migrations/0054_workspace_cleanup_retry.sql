-- Workspace cleanup: a run triggered by an instance termination, and the time of
-- each directory's last attempt, which its retry backoff counts from.
ALTER TABLE workspace_cleanup_runs RENAME TO workspace_cleanup_runs_old;
CREATE TABLE workspace_cleanup_runs (
    id                TEXT PRIMARY KEY,
    trigger           TEXT NOT NULL CHECK (trigger IN ('loop', 'manual', 'terminated')),
    actor             TEXT NOT NULL,
    dry_run           INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    low_disk          INTEGER NOT NULL DEFAULT 0 CHECK (low_disk IN (0, 1)),
    status            TEXT NOT NULL CHECK (status IN ('running', 'finished', 'failed', 'timed_out', 'interrupted')),
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    scanned           INTEGER NOT NULL DEFAULT 0,
    eligible          INTEGER NOT NULL DEFAULT 0,
    preserved         INTEGER NOT NULL DEFAULT 0,
    blocked           INTEGER NOT NULL DEFAULT 0,
    cleaned           INTEGER NOT NULL DEFAULT 0,
    failed            INTEGER NOT NULL DEFAULT 0,
    bytes_estimated   INTEGER NOT NULL DEFAULT 0,
    bytes_freed       INTEGER NOT NULL DEFAULT 0,
    free_before       INTEGER,
    free_after        INTEGER,
    skipped           TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(skipped)),
    error             TEXT
) STRICT;
INSERT INTO workspace_cleanup_runs
SELECT id, trigger, actor, dry_run, low_disk, status, started_at, finished_at,
       scanned, eligible, preserved, blocked, cleaned, failed, bytes_estimated,
       bytes_freed, free_before, free_after, skipped, error
FROM workspace_cleanup_runs_old;
DROP TABLE workspace_cleanup_runs_old;
CREATE INDEX workspace_cleanup_runs_started ON workspace_cleanup_runs (started_at);

ALTER TABLE workspace_cleanup ADD COLUMN attempted_at TEXT;
