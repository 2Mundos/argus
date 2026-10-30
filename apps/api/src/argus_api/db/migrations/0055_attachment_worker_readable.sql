-- Opt-in sharing of an orchestrator attachment with the workers leased on its task
-- (docs/specs/mcp-operations.spec.md). Default 0 keeps attachments manager-only;
-- worker rows never set it.
ALTER TABLE artifacts ADD COLUMN worker_readable INTEGER NOT NULL DEFAULT 0
    CHECK (worker_readable IN (0, 1) AND (worker_readable = 0 OR source = 'attachment'));
