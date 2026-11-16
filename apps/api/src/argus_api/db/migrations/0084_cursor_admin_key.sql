-- A Cursor API-key account may carry a team Admin API key as its own secret,
-- used only to read usage (account usage 3.3). Never an execution credential.
ALTER TABLE accounts ADD COLUMN admin_secret_ref TEXT;
