-- Durable record of who leased which task attempt (worker-read-scope spec).
-- Workers may read a task/artifact only when they have a grant; projects for
-- knowledge and events are derived from grants. Completing a lease does not
-- remove the row.
CREATE TABLE task_lease_grants (
    task_id   TEXT NOT NULL REFERENCES tasks (id),
    agent_id  TEXT NOT NULL,
    attempt   INTEGER NOT NULL,
    leased_at TEXT NOT NULL,
    PRIMARY KEY (task_id, agent_id, attempt)
) STRICT;

CREATE INDEX task_lease_grants_agent ON task_lease_grants (agent_id);
CREATE INDEX task_lease_grants_agent_task ON task_lease_grants (agent_id, task_id);

-- Prior claims already live in the event log.
INSERT INTO task_lease_grants (task_id, agent_id, attempt, leased_at)
SELECT e.subject_id,
       json_extract(e.data, '$.agent_id'),
       CAST(json_extract(e.data, '$.attempt') AS INTEGER),
       e.at
FROM events e
WHERE e.subject_type = 'task'
  AND e.kind = 'task.claim'
  AND json_extract(e.data, '$.agent_id') IS NOT NULL
  AND json_extract(e.data, '$.attempt') IS NOT NULL
  AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = e.subject_id)
ON CONFLICT (task_id, agent_id, attempt) DO NOTHING;
