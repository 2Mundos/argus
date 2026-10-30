-- The automatic resume rule (docs/specs/failure-classes.spec.md 3.2) reads the
-- newest event of one kind for one task (auto resume, progress, retry, unblock)
-- inside the fail transaction; this keeps each read to one index seek.
CREATE INDEX events_task_kind ON events (subject_id, kind, id) WHERE subject_type = 'task';
