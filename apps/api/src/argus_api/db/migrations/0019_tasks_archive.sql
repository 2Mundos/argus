-- Operators archive terminal tasks to take them out of listings and aggregates.
-- Additive and reversible: nothing is deleted, NULL means not archived. Only
-- succeeded, failed and cancelled tasks are archived (enforced by the API).
ALTER TABLE tasks ADD COLUMN archived_at TEXT;
CREATE INDEX tasks_archived ON tasks (archived_at) WHERE archived_at IS NOT NULL;
