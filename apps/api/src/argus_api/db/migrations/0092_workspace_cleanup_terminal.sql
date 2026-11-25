-- Workspace cleanup (workspace-cleanup.spec.md 3.2, 3.8): a blocker that
-- outlived its attempt budget is terminal and carries a decision naming the
-- path and the action; each run records the bytes its archive root holds.
ALTER TABLE workspace_cleanup ADD COLUMN terminal INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0, 1));
ALTER TABLE workspace_cleanup ADD COLUMN decision TEXT;
ALTER TABLE workspace_cleanup_runs ADD COLUMN archive_bytes INTEGER;
