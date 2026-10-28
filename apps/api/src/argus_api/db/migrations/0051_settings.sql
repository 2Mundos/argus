-- Typed global settings with per-project overrides (docs/specs/settings.spec.md).
-- `scope` is `global` (the fleet-wide project row) or a project slug; an absent
-- row means inherit. Values are JSON validated against a registry in code.
CREATE TABLE settings (
    scope      TEXT NOT NULL REFERENCES projects (slug),
    key        TEXT NOT NULL,
    value      TEXT NOT NULL CHECK (json_valid(value)),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (scope, key)
) STRICT, WITHOUT ROWID;

-- Independent code-review child tasks (settings.spec.md 3.5).
INSERT INTO task_kinds (
    name, description, input_schema, result_schema,
    default_capabilities, default_lease_ttl_s, default_max_attempts,
    default_timeout_s, default_review, default_knowledge, created_at, updated_at
) VALUES (
    'code_review',
    'Independent review of code another model delivered: approve or request changes.',
    '{"type":"object","required":["task_id","head_sha"],"additionalProperties":false,"properties":{"task_id":{"type":"string","minLength":1},"head_sha":{"type":"string","pattern":"^[0-9a-f]{7,40}$"},"pr":{"type":["string","null"]}}}',
    '{"type":"object","required":["verdict","findings","head_sha"],"additionalProperties":false,"properties":{"verdict":{"type":"string","enum":["approve","request_changes"]},"findings":{"type":"array","items":{"type":["string","object"]}},"head_sha":{"type":"string","pattern":"^[0-9a-f]{7,40}$"}}}',
    '[]',
    120,
    3,
    3600,
    'none',
    NULL,
    '2026-10-28T00:00:00.000000Z',
    '2026-10-28T00:00:00.000000Z'
);
