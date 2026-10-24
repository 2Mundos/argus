-- Operator steers for the attempt in flight (docs/specs/task-steering.spec.md).
-- A pending steer is delivered on heartbeat, or expires in the same transaction
-- that leaves `claimed`. Rows stay with the task.
CREATE TABLE task_steers (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks (id),
    attempt      INTEGER NOT NULL,
    agent_id     TEXT NOT NULL,
    message      TEXT NOT NULL,
    actor        TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'cancelled', 'expired')),
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    closed_at    TEXT
) STRICT;

CREATE INDEX task_steers_attempt_state ON task_steers (task_id, attempt, state);
