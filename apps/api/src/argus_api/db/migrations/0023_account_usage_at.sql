-- Cost view windows scan usage by time across accounts (cost view spec 5).
CREATE INDEX account_usage_at ON account_usage (at, account_id);
