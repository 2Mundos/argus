-- Fleet health (docs/specs/fleet-health.spec.md) reads a time window of finished
-- tasks and of fail/expire events; without these it would scan history.
CREATE INDEX tasks_finished ON tasks (updated_at) WHERE state IN ('succeeded', 'failed');
CREATE INDEX events_kind_at ON events (kind, at);
