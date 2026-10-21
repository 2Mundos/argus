-- Task complexity gains `hard` (docs/specs/task-complexity.spec.md). SQLite cannot
-- alter a CHECK, so the column is replaced: a new one with the wider CHECK, the values
-- copied, the old one dropped and the new one renamed. Nothing indexes or references
-- `complexity`, so no table rebuild is needed.
ALTER TABLE tasks ADD COLUMN complexity_v2 TEXT NOT NULL DEFAULT 'standard'
    CHECK (complexity_v2 IN ('trivial', 'standard', 'hard'));
UPDATE tasks SET complexity_v2 = complexity;
ALTER TABLE tasks DROP COLUMN complexity;
ALTER TABLE tasks RENAME COLUMN complexity_v2 TO complexity;
