# Worker Read Scope

# 1. Purpose and Scope

Close the IDOR where any valid token could read any task spec, artifact,
decision, knowledge page, or the global event stream. Workers may only read
work they have leased (current or past) and project-scoped resources tied to
that work. Orchestrators and admins keep global reads.

# 2. Non-Goals

- No change to write/lease fencing (`check_lease` for claim holders).
- No change to inbox (already manager-only) or to dependency cross-reads for
  tasks the worker never leased (call that out separately if needed later).
- No new external service.

# 3. Decisions Made

- **Lease history.** Every successful claim inserts a `task_lease_grants` row
  `(task_id, agent_id, attempt, leased_at)`. A worker may read a task (and its
  artifacts / task-scoped decisions / model trace when gated the same way) iff
  they have a grant for that task. Completing or releasing the lease does not
  remove the grant. The key is `(task_id, agent_id, attempt)`: an operator
  retry resets `attempt` to 0, so a later agent can lease the same attempt
  number and must still get its own grant.
- **Managers stay global.** `orchestrator` and `admin` tokens are unchecked for
  these reads. Worker scope alone is narrowed.
- **Projects from leased work.** A worker's allowed projects are the distinct
  `tasks.project` values of tasks they have lease grants for. Knowledge pages,
  proposals, project list entries, and event rows that belong to a project are
  visible only when that project is in the set. Non-project subjects (accounts,
  pools, instances, agents, task kinds) are hidden from workers on the event
  APIs.
- **Events follow leases, not just projects.** `task` events, and `decision`
  events on tasks, are visible to a worker only for tasks it holds a grant for;
  other tasks in the same project stay hidden. `project`, `knowledge_page`, and
  knowledge-proposal `decision` events use the leased-project set. The replay
  (`/events`, `/events/wait`, stream resume) and live stream paths apply the
  same rule.
- **Pinned pages.** A page pinned (`task_knowledge`) on a task the worker has
  leased is readable (page and revisions) even if its project is not leased;
  lists and search stay project-scoped.
- **Lists filter; single GETs refuse.** List/search/wait/stream return only
  allowed rows (empty when the worker has never leased). `GET` by id of a
  disallowed task, artifact, page, or decision returns `403` with
  `code=not_leased` (task/artifact) or `code=project_scope` (knowledge /
  project-bound resources).
- **Edit routes stay manager-only.** `PATCH /tasks/{id}` and MCP `edit_task`
  (and `PUT /tasks/{id}/dependencies`) are refused to every worker, leased or
  not ([task edit](task-edit.spec.md) 3.8); this spec widens nothing. The
  per-attempt definitions (`GET /tasks/{id}/attempts`) carry the spec text and
  follow the task rule: `403 not_leased` for a task the worker never leased.
- **Migration backfill.** Existing `task.claim` events seed `task_lease_grants`
  so prior leases keep working after upgrade.

# 4. Open Questions

None.

# 5. Constraints

- Single process, SQLite, no new service. Grants written inside the claim's
  `Database.write()` transaction with `tx.now`.
- MCP remains a forwarder; REST enforces the scope.

# 6. Implementation Consequences

- Migration `0026_worker_read_scope.sql`: table + backfill.
- `argus_api/access.py`: grant helpers and require_* checks.
- Claim records a grant; task/artifact/decision/knowledge/event routes apply
  scope.
- Negative regression tests for cross-worker access on task spec, attempts,
  artifact, events, knowledge, and decisions, over REST and the real MCP tools,
  plus worker refusal on REST/MCP task edit.
- Supersedes the open "Worker read scope" note in `mcp-operations.spec.md`.
