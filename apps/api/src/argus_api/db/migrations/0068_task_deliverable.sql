-- What a task must leave behind to count as done (docs/specs/task-deliverable.spec.md).
-- Rows that predate the column read `response`, which is what they were held to (a
-- non-empty summary), so no existing task changes behaviour. No CHECK: the API enum is
-- the authority, and a new value then needs no rebuild of `tasks`.
ALTER TABLE tasks ADD COLUMN deliverable TEXT NOT NULL DEFAULT 'response';
