-- Knowledge pages every task of a kind gets on claim (merged with the task's own).
ALTER TABLE task_kinds ADD COLUMN default_knowledge TEXT
    CHECK (default_knowledge IS NULL OR json_valid(default_knowledge));
