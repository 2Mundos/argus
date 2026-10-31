-- Disk guard pause (docs/specs/operations.spec.md 3.5): when the server-side guard
-- paused this claim's session for low disk. Set and cleared only by the guard's
-- service API, never by a worker; cleared on every claim exit and on a new claim.
ALTER TABLE tasks ADD COLUMN paused_disk_at TEXT;
