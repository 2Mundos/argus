-- Task opt-in to project integrations, and integration expiry
-- (docs/specs/integrations.spec.md 3.1, 3.6). Ids only: the secret stays in SOPS.
CREATE TABLE task_integrations (
    task_id        TEXT NOT NULL REFERENCES tasks (id),
    integration_id TEXT NOT NULL REFERENCES integrations (id),
    PRIMARY KEY (task_id, integration_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX task_integrations_integration ON task_integrations (integration_id);

-- When the upstream credential stops working; null: not known. Set on create and
-- rotate (given, or the token's own `exp` claim), checked at every delivery.
ALTER TABLE integrations ADD COLUMN expires_at TEXT;
