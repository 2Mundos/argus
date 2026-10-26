-- Orchestrator attachments (docs/specs/mcp-operations.spec.md): files a manager
-- attaches to a task without a worker lease. They live at the task-level pseudo
-- attempt 0 and are readable only by global readers. Existing rows are worker output.
ALTER TABLE artifacts ADD COLUMN source TEXT NOT NULL DEFAULT 'worker'
    CHECK (source IN ('worker', 'attachment'));
-- The uploading principal (Identity.actor) of an attachment; null for worker rows.
ALTER TABLE artifacts ADD COLUMN attached_by TEXT;
