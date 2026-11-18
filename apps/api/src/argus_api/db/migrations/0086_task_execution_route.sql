-- An explicit execution route chosen for a task at creation (docs/specs/execution-chains.spec.md
-- 3.7): an independent reviewer runs on the `review.chains` alternatives that skip the
-- author's models, instead of the generic chain for its difficulty. NULL = generic chain.
ALTER TABLE tasks ADD COLUMN execution_route TEXT;
