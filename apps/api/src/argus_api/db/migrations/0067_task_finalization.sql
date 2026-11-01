-- Task finalization (docs/specs/task-finalization.spec.md 3.2): when an attempt's
-- claim ended and how the worker's checkpoint follow-up went. All nullable; rows
-- from before this migration have none and read as no finalization.
ALTER TABLE task_attempts ADD COLUMN claim_ended_at TEXT;
ALTER TABLE task_attempts ADD COLUMN claim_end_action TEXT;
ALTER TABLE task_attempts ADD COLUMN checkpoint_state TEXT
  CHECK (checkpoint_state IN ('running', 'saved', 'nothing_to_save', 'failed'));
ALTER TABLE task_attempts ADD COLUMN checkpoint_started_at TEXT;
ALTER TABLE task_attempts ADD COLUMN checkpoint_ended_at TEXT;
ALTER TABLE task_attempts ADD COLUMN checkpoint_detail TEXT;
