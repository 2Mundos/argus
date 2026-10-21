-- Honest usage telemetry (account usage spec): why a run was not measured, and the
-- latest quota probe per account.
-- unmeasured_reason: reporter's short code for a `not_reported` run; NULL = unknown.
ALTER TABLE account_usage ADD COLUMN unmeasured_reason TEXT
    CHECK (unmeasured_reason IS NULL OR length(unmeasured_reason) BETWEEN 1 AND 60);

-- The outcome of the latest quota probe, one row per account (no credential content).
CREATE TABLE account_quota_probe (
    account_id TEXT PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    status     TEXT NOT NULL CHECK (status IN ('updated', 'skipped', 'rate_limited', 'failed')),
    reason     TEXT CHECK (reason IS NULL OR length(reason) <= 200),
    at         TEXT NOT NULL
) STRICT;
