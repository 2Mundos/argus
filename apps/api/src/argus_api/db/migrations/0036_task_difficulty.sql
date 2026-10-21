-- Task difficulty replaces complexity (docs/specs/task-complexity.spec.md): four levels
-- (`simple` is new) and a source of `declared` or `inferred`. Existing rows keep their
-- level; `explicit` becomes `declared`, `heuristic` becomes `inferred`, so a row that
-- predates the field is `standard` / `inferred`. Nothing indexes or references the old
-- columns, so they are dropped.
ALTER TABLE tasks ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'standard'
    CHECK (difficulty IN ('trivial', 'simple', 'standard', 'hard'));
ALTER TABLE tasks ADD COLUMN difficulty_source TEXT NOT NULL DEFAULT 'inferred'
    CHECK (difficulty_source IN ('declared', 'inferred'));
UPDATE tasks SET difficulty = complexity,
    difficulty_source = CASE complexity_source WHEN 'explicit' THEN 'declared' ELSE 'inferred' END;
ALTER TABLE tasks DROP COLUMN complexity;
ALTER TABLE tasks DROP COLUMN complexity_source;
