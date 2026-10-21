-- Per-task model pins (docs/specs/task-models.spec.md): a JSON object
-- {engine, model} / {executor, model}, NULL when the task follows the policy (every
-- task that predates it). Validated by the API.
ALTER TABLE tasks ADD COLUMN harness_model TEXT
    CHECK (harness_model IS NULL OR (json_valid(harness_model)
        AND json_type(harness_model) = 'object'));
ALTER TABLE tasks ADD COLUMN executor_model TEXT
    CHECK (executor_model IS NULL OR (json_valid(executor_model)
        AND json_type(executor_model) = 'object'));
