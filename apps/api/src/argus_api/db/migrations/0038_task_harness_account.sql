-- Per-task harness account pin (docs/specs/task-models.spec.md 3.4): the id of the
-- one account the task's engine lease may use, NULL when any account may (every task
-- that predates it). No foreign key: a deleted account leaves the task waiting as
-- `awaiting_quota` until the pin is edited. Validated by the API.
ALTER TABLE tasks ADD COLUMN harness_account TEXT;
