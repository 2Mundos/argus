-- The caller's request id for an answer, so a retry of the same answer is
-- replayed instead of refused (MCP operations 3.2).
ALTER TABLE decisions ADD COLUMN request_id TEXT;
