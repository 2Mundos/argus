-- An attempt an operator released (docs/specs/worker-lifecycle.spec.md 3.3): the task
-- went back to ready without counting a failure. Other endings live in the events.
ALTER TABLE task_attempts ADD COLUMN outcome TEXT CHECK (outcome IN ('released'));
ALTER TABLE task_attempts ADD COLUMN outcome_reason TEXT;
