-- Structured progress milestones (docs/specs/progress-milestones.spec.md).
ALTER TABLE tasks ADD COLUMN progress_kind TEXT;
ALTER TABLE tasks ADD COLUMN progress_data TEXT CHECK (progress_data IS NULL OR json_valid(progress_data));
