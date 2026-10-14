-- A wall-clock cap on one attempt, separate from the heartbeat-renewed lease.
-- timeout_s is the configured cap (NULL: use the kind's, then the global default);
-- timeout_at is the current attempt's deadline, set at claim and cleared when the
-- task leaves `claimed`. Neither renewals nor progress notes move it.
ALTER TABLE tasks ADD COLUMN timeout_s INTEGER CHECK (timeout_s IS NULL OR timeout_s > 0);
ALTER TABLE tasks ADD COLUMN timeout_at TEXT;
CREATE INDEX tasks_timeout ON tasks (timeout_at) WHERE state = 'claimed' AND timeout_at IS NOT NULL;

ALTER TABLE task_kinds ADD COLUMN default_timeout_s INTEGER
    CHECK (default_timeout_s IS NULL OR default_timeout_s > 0);
