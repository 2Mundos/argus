-- Stall causes (stall-visibility.spec.md 3.7): the newest lease observation a
-- worker host sent on heartbeat (JSON) and when Argus received it.
ALTER TABLE tasks ADD COLUMN observation TEXT;
ALTER TABLE tasks ADD COLUMN observation_at TEXT;
