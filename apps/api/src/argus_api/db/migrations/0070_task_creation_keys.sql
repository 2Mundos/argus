-- Idempotency-Key on task creation (docs/specs/worker-lifecycle.spec.md 3.7): a
-- retry with the same actor, key and request gets the task it created back.
CREATE TABLE IF NOT EXISTS task_creation_keys (
    actor           TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest  TEXT NOT NULL,
    task_id         TEXT NOT NULL REFERENCES tasks (id),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (actor, idempotency_key)
) STRICT;
