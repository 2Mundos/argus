-- Quota from every provider that has a readable source (account usage spec 3.3):
-- a machine-readable code for why a probe did not update, and provider credit as
-- its own quantity (money, not a window percentage).
ALTER TABLE account_quota_probe ADD COLUMN code TEXT
    CHECK (code IS NULL OR length(code) BETWEEN 1 AND 40);

-- The latest credit reading per account (an OpenRouter key's limit, Cursor grants).
-- NULL limit_amount = the key has no cap; NULL remaining_amount with a limit is
-- never stored (it is unknown, so no row). Amounts are in `unit`, never summed
-- with token counts.
CREATE TABLE account_credit (
    account_id       TEXT PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    unit             TEXT NOT NULL CHECK (unit IN ('usd')),
    limit_amount     REAL CHECK (limit_amount IS NULL OR limit_amount >= 0),
    remaining_amount REAL CHECK (remaining_amount IS NULL OR remaining_amount >= 0),
    used_amount      REAL CHECK (used_amount IS NULL OR used_amount >= 0),
    limit_reset      TEXT CHECK (limit_reset IS NULL OR length(limit_reset) <= 20),
    resets_at        TEXT,
    free_tier        INTEGER CHECK (free_tier IS NULL OR free_tier IN (0, 1)),
    observed_at      TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('polled'))
) STRICT;
