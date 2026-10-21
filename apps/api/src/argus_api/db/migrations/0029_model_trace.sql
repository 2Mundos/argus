-- Model observability (docs/specs/model-observability.spec.md). Additive: tasks that
-- predate it simply have no rows, and a missing model is NULL, never inferred.
-- One row per model run of a layer (harness | delegate). `key` is the producer's id of
-- the run, so a re-sent report completes the same row instead of adding another.
CREATE TABLE model_executions (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks (id),
    key              TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    layer            TEXT NOT NULL CHECK (layer IN ('harness', 'delegate')),
    state            TEXT NOT NULL CHECK (state IN
        ('selected', 'running', 'succeeded', 'failed', 'cancelled')),
    agent_id         TEXT,
    runtime          TEXT,
    engine           TEXT,
    executor         TEXT,
    provider         TEXT,
    requested_model  TEXT,
    configured_model TEXT,
    observed_model   TEXT,
    evidence         TEXT NOT NULL DEFAULT 'not_reported' CHECK (evidence IN
        ('reported_by_runtime', 'cli_output', 'provider_response', 'not_reported')),
    policy_revision  TEXT,
    account_id       TEXT,
    delegation_id    TEXT,
    run_key          TEXT,
    resumed_from     TEXT,
    error_code       TEXT,
    started_at       TEXT,
    ended_at         TEXT,
    recorded_at      TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CHECK ((observed_model IS NULL) = (evidence = 'not_reported')),
    UNIQUE (task_id, key)
) STRICT;
CREATE INDEX model_executions_agent ON model_executions (agent_id, recorded_at);

-- The routing timeline: initial selection and every switch, in both layers.
CREATE TABLE model_selections (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks (id),
    key              TEXT NOT NULL,
    attempt          INTEGER NOT NULL,
    layer            TEXT NOT NULL CHECK (layer IN ('harness', 'delegate')),
    kind             TEXT NOT NULL CHECK (kind IN ('initial', 'account_switch',
        'model_switch', 'provider_switch', 'executor_switch', 'retry_no_fallback')),
    reason_code      TEXT NOT NULL CHECK (reason_code IN ('priority', 'availability',
        'compatibility', 'context', 'quota', 'error', 'policy')),
    explanation      TEXT,
    from_model       TEXT,
    to_model         TEXT,
    from_provider    TEXT,
    to_provider      TEXT,
    from_executor    TEXT,
    to_executor      TEXT,
    from_account_id  TEXT,
    to_account_id    TEXT,
    alternatives     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(alternatives)),
    execution_key    TEXT,
    policy_revision  TEXT,
    outcome          TEXT NOT NULL DEFAULT 'unknown' CHECK (outcome IN
        ('unknown', 'succeeded', 'failed', 'abandoned')),
    agent_id         TEXT,
    at               TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    UNIQUE (task_id, key)
) STRICT;
