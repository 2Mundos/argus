-- In-flight executor usage, one row per delegated run (task cost spec 3.8).
-- Cumulative figures at `revision`; replaced (deleted) by the run's final measured
-- usage row in the same transaction. interrupted: a final not_reported row came.
CREATE TABLE usage_provisional (
    task_id             TEXT NOT NULL,
    run_key             TEXT NOT NULL,
    attempt             INTEGER NOT NULL CHECK (attempt >= 1),
    account_id          TEXT NOT NULL REFERENCES accounts (id),
    agent_id            TEXT NOT NULL,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    model               TEXT,
    engine              TEXT,
    input_tokens        INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens       INTEGER NOT NULL CHECK (output_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL CHECK (cached_input_tokens >= 0),
    interrupted         INTEGER NOT NULL DEFAULT 0 CHECK (interrupted IN (0, 1)),
    at                  TEXT NOT NULL,
    PRIMARY KEY (task_id, run_key)
) STRICT;
