-- Durable traversal; snapshots contain identifiers only, never credentials.
CREATE TABLE task_execution_routes (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
    generation INTEGER NOT NULL DEFAULT 0,
    selection TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    skips TEXT NOT NULL DEFAULT '[]'
);
