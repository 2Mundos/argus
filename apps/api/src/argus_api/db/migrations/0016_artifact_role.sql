-- What an artifact is for, declared at upload and never inferred from its name
-- (MCP operations 3.2). Artifacts uploaded before this have no role.
ALTER TABLE artifacts ADD COLUMN role TEXT
    CHECK (role IN ('report', 'delegate_report', 'policy_snapshot', 'qa_receipt', 'other'));
