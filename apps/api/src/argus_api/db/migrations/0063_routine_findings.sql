-- Routine finding dedup (docs/specs/routines.spec.md 3.3): finding tasks carry
-- `input.routine_finding = {routine, key}`; intake looks them up by that pair.
CREATE INDEX tasks_routine_finding ON tasks (
    json_extract(input, '$.routine_finding.routine'),
    json_extract(input, '$.routine_finding.key')
);
