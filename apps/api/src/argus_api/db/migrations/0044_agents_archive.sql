-- Operators archive ended (lost/gone) agent identities so they leave listings
-- and aggregates. Additive: nothing is deleted, NULL means not archived.
-- Archiving never changes state, ended_at, end_reason or last_heartbeat_at.
ALTER TABLE agents ADD COLUMN archived_at TEXT;
CREATE INDEX agents_archived ON agents (archived_at) WHERE archived_at IS NOT NULL;
