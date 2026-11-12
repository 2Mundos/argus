-- Project-scoped integrations (docs/specs/integrations.spec.md). The secret never
-- lives here: secret_ref points into the SOPS file and is cleared on revoke.
CREATE TABLE integrations (
    id                TEXT PRIMARY KEY,
    project           TEXT NOT NULL REFERENCES projects (slug),
    kind              TEXT NOT NULL CHECK (kind IN ('mcp', 'kubernetes')),
    name              TEXT NOT NULL,
    config            TEXT NOT NULL CHECK (json_valid(config)),
    access            TEXT NOT NULL CHECK (access IN ('read_only', 'read_write')),
    secret_ref        TEXT,
    secret_version    INTEGER NOT NULL DEFAULT 1,
    state             TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
    validation        TEXT NOT NULL DEFAULT 'unchecked'
        CHECK (validation IN ('unchecked', 'ok', 'failed')),
    validation_detail TEXT,
    validated_at      TEXT,
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    rotated_at        TEXT,
    revoked_at        TEXT,
    revoked_by        TEXT,
    CHECK ((state = 'active') = (secret_ref IS NOT NULL)),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX integrations_active_name ON integrations (project, name)
    WHERE state = 'active';
CREATE INDEX integrations_project ON integrations (project, state);
