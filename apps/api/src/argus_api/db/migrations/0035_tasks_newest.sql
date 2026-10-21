-- Newest-first task lists (docs/specs/mcp-operations.spec.md) filter on
-- archived_at and order by (created_at, id). These partial indexes cover that
-- scan and the matching COUNT(*).
CREATE INDEX tasks_active_newest ON tasks (created_at, id) WHERE archived_at IS NULL;
CREATE INDEX tasks_archived_newest ON tasks (created_at, id) WHERE archived_at IS NOT NULL;
