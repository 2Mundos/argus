-- Track per-alternative failed account IDs across the current unresolved quota window.
ALTER TABLE task_execution_routes ADD COLUMN failed_accounts TEXT NOT NULL DEFAULT '{}';
