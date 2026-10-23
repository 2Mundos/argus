-- The task continuation contract (docs/specs/task-continuation.spec.md): artifacts,
-- WIP, interruption, checklist evidence and effective decisions, as one JSON document
-- fenced by its own `revision`. NULL for every task that predates it: such a task
-- reads as an unstructured contract with explicit gaps, never as "all done".
ALTER TABLE tasks ADD COLUMN continuation TEXT CHECK (continuation IS NULL OR json_valid(continuation));
