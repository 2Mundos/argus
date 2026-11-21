-- A code_review may fix its findings on the PR branch (pre-merge independent review
-- spec 3.7): input carries the scoped `fix` fence, result takes `fixed` plus the new head.
UPDATE task_kinds SET
    description = 'Independent review of code another model delivered: approve, request changes, or fix the findings on the PR branch.',
    input_schema = '{"type":"object","required":["task_id","head_sha"],"additionalProperties":false,"properties":{"task_id":{"type":"string","minLength":1},"head_sha":{"type":"string","pattern":"^[0-9a-f]{7,40}$"},"pr":{"type":["string","null"]},"fix":{"type":"object","additionalProperties":false,"properties":{"pr":{"type":["string","null"]},"base_head_sha":{"type":"string","pattern":"^[0-9a-f]{7,40}$"},"force_push":{"const":false}}}}}',
    result_schema = '{"type":"object","required":["verdict","findings","head_sha"],"additionalProperties":false,"properties":{"verdict":{"type":"string","enum":["approve","request_changes","fixed"]},"findings":{"type":"array","items":{"type":["string","object"]}},"head_sha":{"type":"string","pattern":"^[0-9a-f]{7,40}$"},"fixed_head_sha":{"type":"string","pattern":"^[0-9a-f]{40}$"},"tests":{"type":"array","items":{"type":["string","object"]}}}}'
WHERE name = 'code_review';
