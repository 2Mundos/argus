-- Idempotent, honest usage rows and optional local token limits (account usage spec).
-- run_key: client-built key of one run; a repeated report adds no row.
ALTER TABLE account_usage ADD COLUMN run_key TEXT;
-- not_reported: the run happened but the engine gave no figures (all NULL, never 0).
ALTER TABLE account_usage ADD COLUMN measurement TEXT NOT NULL DEFAULT 'measured'
    CHECK (measurement IN ('measured', 'not_reported'));
CREATE UNIQUE INDEX account_usage_run_key ON account_usage (account_id, run_key)
    WHERE run_key IS NOT NULL;

-- JSON object of optional max total tokens per rolling window: "5h", "24h", "7d".
ALTER TABLE accounts ADD COLUMN token_limits TEXT
    CHECK (token_limits IS NULL OR json_valid(token_limits));
