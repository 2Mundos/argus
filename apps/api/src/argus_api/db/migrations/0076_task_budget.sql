-- Optional per-task budget (task cost spec 3.4, 3.5).
-- budget_usd: estimated spend cap in USD; NULL means no budget.
-- budget_steered_attempt: the attempt that already got its budget steer.
ALTER TABLE tasks ADD COLUMN budget_usd REAL;
ALTER TABLE tasks ADD COLUMN budget_steered_attempt INTEGER;
