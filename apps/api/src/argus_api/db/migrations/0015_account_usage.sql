-- Account usage: richer usage rows, quota snapshots, billing day (account usage spec).
ALTER TABLE account_usage ADD COLUMN cached_input_tokens INTEGER;
ALTER TABLE account_usage ADD COLUMN model TEXT;
ALTER TABLE account_usage ADD COLUMN engine TEXT;
ALTER TABLE account_usage ADD COLUMN role TEXT
    CHECK (role IS NULL OR role IN ('engine', 'executor'));

-- Day of the month the provider resets monthly quotas (Cursor); estimates only.
ALTER TABLE accounts ADD COLUMN billing_day INTEGER
    CHECK (billing_day IS NULL OR billing_day BETWEEN 1 AND 31);

-- The latest quota snapshot per account and window: "<minutes>m" for timed
-- windows, a pool name for two-state pools (used_percent NULL).
CREATE TABLE account_quota (
    account_id      TEXT NOT NULL REFERENCES accounts (id),
    window          TEXT NOT NULL,
    window_minutes  INTEGER CHECK (window_minutes IS NULL OR window_minutes > 0),
    used_percent    REAL CHECK (used_percent IS NULL OR used_percent BETWEEN 0 AND 100),
    resets_at       TEXT,
    exhausted_until TEXT,
    observed_at     TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('reported', 'polled', 'estimated')),
    PRIMARY KEY (account_id, window)
) STRICT;
