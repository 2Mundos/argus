-- Every task belongs to a project. Enforced by the API (a subtask inherits its
-- parent's); SQLite cannot add NOT NULL to an existing column without rebuilding
-- a table that others reference, so existing rows are backfilled here.
UPDATE tasks SET project = 'global' WHERE project IS NULL;
