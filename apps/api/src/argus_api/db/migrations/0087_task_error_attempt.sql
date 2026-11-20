-- Which attempt reported `tasks.error`, and when (docs/specs/task-models.spec.md 3.5).
-- The error text survives a requeue and the next claim, so readers need the attempt to
-- tell a current failure from a previous one. NULL = no error, or one recorded before
-- this migration whose attempt is unknown.
ALTER TABLE tasks ADD COLUMN error_attempt INTEGER;
ALTER TABLE tasks ADD COLUMN error_at TEXT;

-- An earlier error belongs to the attempt whose claim ended last.
UPDATE tasks SET
  error_attempt = (SELECT a.attempt FROM task_attempts a
                    WHERE a.task_id = tasks.id AND a.claim_ended_at IS NOT NULL
                    ORDER BY a.id DESC LIMIT 1),
  error_at = (SELECT a.claim_ended_at FROM task_attempts a
               WHERE a.task_id = tasks.id AND a.claim_ended_at IS NOT NULL
               ORDER BY a.id DESC LIMIT 1)
 WHERE error IS NOT NULL;
