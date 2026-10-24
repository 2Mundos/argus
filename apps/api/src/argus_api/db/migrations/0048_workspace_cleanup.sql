-- Workspace cleanup of terminated NanoClaw instance directories
-- (workspace-cleanup.spec.md 3.8). One row per run, one audit row per scanned
-- directory entry (root, name); error columns hold sanitized reason text only.
CREATE TABLE workspace_cleanup_runs (
    id                TEXT PRIMARY KEY,
    trigger           TEXT NOT NULL CHECK (trigger IN ('loop', 'manual')),
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

CREATE INDEX workspace_cleanup_runs_started ON workspace_cleanup_runs (started_at);

CREATE TABLE workspace_cleanup (
    root            TEXT NOT NULL,
    name            TEXT NOT NULL,
    instance_id     TEXT,
    pool            TEXT,
    status          TEXT NOT NULL CHECK (status IN ('eligible', 'preserved', 'blocked', 'cleaned', 'failed')),
    reason          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    bytes_estimated INTEGER,
    bytes_freed     INTEGER,
    archive_path    TEXT,
    error           TEXT,
    lock_run_id     TEXT,
    lock_expires_at TEXT,
    last_run_id     TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    cleaned_at      TEXT,
    PRIMARY KEY (root, name)
) STRICT;

CREATE INDEX workspace_cleanup_status ON workspace_cleanup (status, updated_at);
