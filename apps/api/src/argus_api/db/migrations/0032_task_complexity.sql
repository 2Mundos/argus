-- Task complexity (docs/specs/task-complexity.spec.md): `trivial` tasks prefer the local
-- model. Rows that predate it are `standard`, and their source is `heuristic`: the
-- creator never chose.
ALTER TABLE tasks ADD COLUMN complexity TEXT NOT NULL DEFAULT 'standard'
    CHECK (complexity IN ('trivial', 'standard'));
ALTER TABLE tasks ADD COLUMN complexity_source TEXT NOT NULL DEFAULT 'heuristic'
    CHECK (complexity_source IN ('explicit', 'heuristic'));
