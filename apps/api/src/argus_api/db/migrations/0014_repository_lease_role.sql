-- Repository credentials are independent from engine and executor accounts.
ALTER TABLE account_leases RENAME TO account_leases_old;
CREATE TABLE account_leases (
    id             TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES accounts (id),
    agent_id       TEXT NOT NULL REFERENCES agents (id),
    providers      TEXT NOT NULL CHECK (json_valid(providers)),
    leased_at      TEXT NOT NULL,
    released_at    TEXT,
    release_reason TEXT,
    role           TEXT NOT NULL DEFAULT 'engine'
                   CHECK (role IN ('engine', 'executor', 'repository'))
) STRICT;
INSERT INTO account_leases
    (id, account_id, agent_id, providers, leased_at, released_at, release_reason, role)
SELECT id, account_id, agent_id, providers, leased_at, released_at, release_reason, role
FROM account_leases_old;
DROP TABLE account_leases_old;
CREATE UNIQUE INDEX account_leases_active_agent_role ON account_leases (agent_id, role)
    WHERE released_at IS NULL;
CREATE INDEX account_leases_active_account ON account_leases (account_id)
    WHERE released_at IS NULL;
