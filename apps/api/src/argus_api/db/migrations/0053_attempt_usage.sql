-- One usage outcome per task attempt (account usage spec 3.10).
-- attempt: the task attempt a run belongs to; NULL when it cannot be attributed.
-- account_id becomes nullable: an attempt placeholder with no known account.
-- SQLite cannot drop NOT NULL in place, so the table is rebuilt with its ids.
ALTER TABLE account_usage RENAME TO account_usage_old;
CREATE TABLE account_usage (
    id                  INTEGER PRIMARY KEY,
    account_id          TEXT REFERENCES accounts (id),
    agent_id            TEXT,
    task_id             TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cost_usd            REAL,
    at                  TEXT NOT NULL,
    cached_input_tokens INTEGER,
    model               TEXT,
    engine              TEXT,
    role                TEXT CHECK (role IS NULL OR role IN ('engine', 'executor')),
    run_key             TEXT,
    measurement         TEXT NOT NULL DEFAULT 'measured'
                        CHECK (measurement IN ('measured', 'not_reported')),
    unmeasured_reason   TEXT
                        CHECK (unmeasured_reason IS NULL OR length(unmeasured_reason) BETWEEN 1 AND 60),
    attempt             INTEGER CHECK (attempt IS NULL OR attempt >= 1)
) STRICT;
-- Existing rows take the reporting agent's latest claim of the task at or before
-- the row (none: NULL, counted only in totals).
INSERT INTO account_usage
    (id, account_id, agent_id, task_id, input_tokens, output_tokens, cost_usd, at,
     cached_input_tokens, model, engine, role, run_key, measurement, unmeasured_reason,
     attempt)
SELECT u.id, u.account_id, u.agent_id, u.task_id, u.input_tokens, u.output_tokens,
       u.cost_usd, u.at, u.cached_input_tokens, u.model, u.engine, u.role, u.run_key,
       u.measurement, u.unmeasured_reason,
       (SELECT a.attempt FROM task_attempts a
        WHERE a.task_id = u.task_id AND a.claimed_at <= u.at AND a.attempt >= 1
          AND (u.agent_id IS NULL OR a.agent_id = u.agent_id)
        ORDER BY a.id DESC LIMIT 1)
FROM account_usage_old u;
DROP TABLE account_usage_old;
CREATE INDEX account_usage_account ON account_usage (account_id, at);
CREATE UNIQUE INDEX account_usage_run_key ON account_usage (account_id, run_key)
    WHERE run_key IS NOT NULL;
CREATE INDEX account_usage_at ON account_usage (at, account_id);
CREATE INDEX account_usage_task ON account_usage (task_id, attempt) WHERE task_id IS NOT NULL;
-- The API's own placeholder key, a1:<task>:<attempt>:<claim>, is unique whatever
-- the account (which may be NULL, where the index above does not apply).
CREATE UNIQUE INDEX account_usage_attempt_outcome ON account_usage (run_key)
    WHERE substr(run_key, 1, 3) = 'a1:';
