-- Unknown steer outcomes (docs/specs/task-steering.spec.md 3.3-3.4): a steer records
-- when a heartbeat first carried it, and one dispatched but never acknowledged ends
-- `unknown` with its attempt. The attempt keeps how many ended that way, for the next.
-- Dispatch was not recorded before this version, so a steer still pending at upgrade
-- may already have been carried: it is marked dispatched, and ends `unknown`, never
-- `expired`, when its attempt ends.
ALTER TABLE task_steers RENAME TO task_steers_old;
CREATE TABLE task_steers (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks (id),
    attempt       INTEGER NOT NULL,
    agent_id      TEXT NOT NULL,
    message       TEXT NOT NULL,
    actor         TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'cancelled', 'expired', 'unknown')),
    created_at    TEXT NOT NULL,
    dispatched_at TEXT,
    delivered_at  TEXT,
    closed_at     TEXT
) STRICT;
INSERT INTO task_steers
    (id, task_id, attempt, agent_id, message, actor, state, created_at,
     dispatched_at, delivered_at, closed_at)
SELECT id, task_id, attempt, agent_id, message, actor, state, created_at,
       CASE WHEN state = 'pending' THEN created_at END, delivered_at, closed_at
FROM task_steers_old;
DROP TABLE task_steers_old;
CREATE INDEX task_steers_attempt_state ON task_steers (task_id, attempt, state);

ALTER TABLE task_attempts ADD COLUMN steer_unknown INTEGER NOT NULL DEFAULT 0;
