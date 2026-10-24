-- Stall visibility: last worker activity and when a claimed task was flagged quiet.
ALTER TABLE tasks ADD COLUMN activity_at TEXT;
ALTER TABLE tasks ADD COLUMN quiet_at TEXT;
