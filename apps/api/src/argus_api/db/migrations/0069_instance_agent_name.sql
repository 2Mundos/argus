-- Display name for the NanoClaw host / OneCLI agent of this instance
-- (docs/specs/worker-launch.spec.md). Assigned at reserve from the mythology
-- word list; NULL on rows that predate this column (cleanup falls back to
-- <pool>-<id[:8]>).
ALTER TABLE instances ADD COLUMN agent_name TEXT;
