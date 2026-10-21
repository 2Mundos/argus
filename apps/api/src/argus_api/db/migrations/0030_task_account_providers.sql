-- Per-task provider restriction (docs/specs/local-harness.spec.md 3): a JSON array of
-- engine-capable account providers the task may run on, NULL when unrestricted (every
-- task that predates it). Validated by the API; the array is never empty.
ALTER TABLE tasks ADD COLUMN account_providers TEXT
    CHECK (account_providers IS NULL OR (json_valid(account_providers)
        AND json_type(account_providers) = 'array'
        AND json_array_length(account_providers) > 0));
