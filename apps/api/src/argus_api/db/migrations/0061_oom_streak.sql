-- OOM promotion (docs/specs/failure-classes.spec.md 3.2.1). `tasks.oom_kill` is 1
-- when the attempt that ended last was OOM-killed; `pools.oom_streak` counts the
-- pool's consecutive OOM-killed session exits.
ALTER TABLE tasks ADD COLUMN oom_kill INTEGER NOT NULL DEFAULT 0 CHECK (oom_kill IN (0, 1));
ALTER TABLE pools ADD COLUMN oom_streak INTEGER NOT NULL DEFAULT 0 CHECK (oom_streak >= 0);
