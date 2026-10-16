-- An agent may hold one engine lease (the account its harness runs on) and one
-- executor lease (the account a delegated CLI uses), instead of a single lease.
-- Codex accounts spec 3.
ALTER TABLE account_leases ADD COLUMN role TEXT NOT NULL DEFAULT 'engine'
    CHECK (role IN ('engine', 'executor'));

DROP INDEX account_leases_active_agent;
CREATE UNIQUE INDEX account_leases_active_agent_role ON account_leases (agent_id, role)
    WHERE released_at IS NULL;
