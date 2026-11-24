-- argus: foreign keys off
-- Forge integrations (docs/specs/forge-integrations.spec.md 3.2, 3.6): repository
-- credentials move from accounts to a per-project integration of kind `forge`.
-- Rebuilds `integrations` with the widened kind check and every column added since
-- 0080; `task_integrations` keeps referencing it by name. One active forge record
-- per project. Moving the tokens needs the secret store, so the API does it at start.
CREATE TABLE integrations_new (
    id                TEXT PRIMARY KEY,
    project           TEXT NOT NULL REFERENCES projects (slug),
    kind              TEXT NOT NULL CHECK (kind IN ('mcp', 'kubernetes', 'forge')),
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
    expires_at        TEXT,
    config_version    INTEGER NOT NULL DEFAULT 1,
    CHECK ((state = 'active') = (secret_ref IS NOT NULL)),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
) STRICT;
INSERT INTO integrations_new
    (id, project, kind, name, config, access, secret_ref, secret_version, state,
     validation, validation_detail, validated_at, created_at, created_by, updated_at,
     rotated_at, revoked_at, revoked_by, expires_at, config_version)
SELECT id, project, kind, name, config, access, secret_ref, secret_version, state,
       validation, validation_detail, validated_at, created_at, created_by, updated_at,
       rotated_at, revoked_at, revoked_by, expires_at, config_version
FROM integrations;
DROP TABLE integrations;
ALTER TABLE integrations_new RENAME TO integrations;
CREATE UNIQUE INDEX integrations_active_name ON integrations (project, name)
    WHERE state = 'active';
CREATE INDEX integrations_project ON integrations (project, state);
CREATE UNIQUE INDEX integrations_active_forge ON integrations (project)
    WHERE state = 'active' AND kind = 'forge';
