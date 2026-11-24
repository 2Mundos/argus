-- Structured review findings and the read-only findings verifier (review finding
-- verification spec 3.1, 3.4): the code_review result takes finding objects, and the
-- new kind `review_verification` carries the verifier's answers. No new table.
UPDATE task_kinds SET
    result_schema = '{"type":"object","required":["verdict","findings","head_sha"],"additionalProperties":false,"properties":{"verdict":{"type":"string","enum":["approve","request_changes","fixed"]},"findings":{"type":"array","items":{"type":["string","object"]}},"head_sha":{"type":"string","pattern":"^[0-9a-f]{7,40}$"},"fixed_head_sha":{"type":"string","pattern":"^[0-9a-f]{40}$"},"tests":{"type":"array","items":{"type":["string","object"]}}},"allOf":[{"if":{"required":["verdict"],"properties":{"verdict":{"enum":["request_changes","fixed"]}}},"then":{"properties":{"findings":{"items":{"type":"object","additionalProperties":false,"required":["id","priority","file","title","description","evidence"],"properties":{"id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,40}$"},"priority":{"enum":["P1","P2","P3"]},"file":{"type":"string","minLength":1,"maxLength":500},"lines":{"type":"string","pattern":"^[0-9]+(-[0-9]+)?$"},"title":{"type":"string","minLength":1,"maxLength":120,"pattern":"^[^\\n]+$"},"description":{"type":"string","minLength":1,"maxLength":2000},"evidence":{"type":"object","additionalProperties":false,"required":["kind","detail"],"properties":{"kind":{"enum":["test","reproduction","reading"]},"detail":{"type":"string","minLength":1,"maxLength":2000}}},"repeat_of":{"type":"string","pattern":"^[^:\\s]+:[^:\\s]+$"}}}}}}}]}'
WHERE name = 'code_review';

INSERT INTO task_kinds (
    name, description, input_schema, result_schema,
    default_capabilities, default_lease_ttl_s, default_max_attempts,
    default_timeout_s, default_review, default_knowledge, created_at, updated_at
) VALUES (
    'review_verification',
    'Read-only check of an independent review''s findings: confirm, refute or leave unclear.',
    '{"type":"object","required":["review_child_id","head_sha","findings"],"additionalProperties":false,"properties":{"review_child_id":{"type":"string","minLength":1},"pr":{"type":["string","null"]},"head_sha":{"type":"string","pattern":"^[0-9a-f]{7,40}$"},"findings":{"type":"array","items":{"type":"object"}}}}',
    '{"type":"object","required":["verifications"],"additionalProperties":false,"properties":{"verifications":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["id","status","evidence"],"properties":{"id":{"type":"string","minLength":1},"status":{"enum":["confirmed","refuted","unclear"]},"evidence":{"type":"string","minLength":1,"maxLength":2000}}}}}}',
    '[]',
    120,
    3,
    3600,
    'none',
    NULL,
    '2026-11-24T00:00:00.000000Z',
    '2026-11-24T00:00:00.000000Z'
);
