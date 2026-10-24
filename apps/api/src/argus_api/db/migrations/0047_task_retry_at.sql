-- Earliest time a requeued task may be claimed again: the backoff after a
-- transient failure (failure classes 3.2). NULL means at once.
ALTER TABLE tasks ADD COLUMN retry_at TEXT;
