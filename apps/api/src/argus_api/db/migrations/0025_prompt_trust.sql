-- Immutable origin of task content for prompt-data isolation.
-- Worker-created subtasks are always 'worker' and never elevate to 'orchestrator'.
ALTER TABLE tasks ADD COLUMN prompt_trust TEXT NOT NULL DEFAULT 'orchestrator'
    CHECK (prompt_trust IN ('orchestrator', 'worker'));
