# Task dependencies

# 1. Purpose and Scope

Dependencies are the only control flow between tasks
([worker lifecycle](worker-lifecycle.spec.md) 3.2): a task with unfinished
dependencies waits in `pending` and becomes `ready` once all of them
`succeeded`. This spec closes three gaps in that model:

1. **Editing.** Dependencies are fixed at creation. An orchestrator cannot add a
   prerequisite it forgot or drop one that no longer matters, although the
   scope table (worker lifecycle 3.6) says orchestrators "edit deps".
2. **Visibility.** A dependent of a `failed` or `cancelled` task stays `pending`
   with only a `task.dependency_failed` event. The inbox does not show it, so
   it waits unnoticed.
3. **Stranding.** A `cancelled` dependency can never succeed and cannot be
   removed, so its dependents can only be cancelled or recreated.

# 2. Non-Goals

- No cascading: Argus still never cancels or fails a dependent on its own.
- No ordering by shared resource (one task at a time per repository, say).
  Order between tasks is declared, never inferred. That is a separate spec.
- No conditional dependencies ("run if A failed"). A dependency means
  "succeeded first".
- No editing once a task was claimed: from `claimed` on, its work may already
  rely on what its dependencies produced.

# 3. Decisions Made

## 3.1 Replacing dependencies

- `PUT /api/tasks/{id}/dependencies` with `{depends_on: [ids], expected_version}`
  (orchestrator or admin) → MCP `set_task_dependencies`. It replaces the whole
  set; the response is the task, and its `version` changes.
- Allowed while the task is `pending`, `awaiting_approval` or `ready`; otherwise
  `409 task_not_waiting`. `expected_version` is the task's `version` (its
  `updated_at`, MCP operations 3.1); a mismatch is `409 stale_version` with the
  current version in `details`.
- Every id must exist (`404`), the task cannot depend on itself, and the new set
  cannot close a cycle: if a new dependency already depends on the task,
  directly or transitively, it is `422 dependency_cycle` with the path in
  `details`. At most 100 dependencies.
- The state follows the new set in the same transaction: a `ready` task with an
  unfinished dependency goes back to `pending` (a new `deps_added` action, the
  only way back from `ready`; nothing holds a lease in `ready`); a `pending` task
  whose dependencies all `succeeded` becomes `ready` (`deps_satisfied`, as
  today). `awaiting_approval` keeps its state; approval already checks the
  dependencies.
- It records `task.dependencies_changed` with `added` and `removed`. A newly
  added dependency that is already `failed` or `cancelled` raises
  `task.dependency_failed`, as at creation.
- Removing a `cancelled` or `failed` dependency is how a stranded task is
  released (gap 3): no other mechanism is added.

## 3.2 Stuck dependents in the inbox

- The inbox (MCP operations 3.2) also lists `pending` tasks with at least one
  `failed` or `cancelled` dependency. Their row has no decision and carries
  `failed_dependencies: [ids]`; every other row has an empty list. They wait
  since the latest of those dependencies ended.
- `states` accepts `pending` for these rows; they count under `pending` in
  `totals_by_state` and under `none` in `totals_by_decision_kind`.
- The orchestrator then retries the dependency (`retry_task`, if `failed`),
  replaces the dependencies (3.1), or cancels the dependent.

# 4. Open Questions

- Whether a dependent's claim should carry its dependencies' result summaries,
  so a worker does not have to read them with `get_tasks`. Decide once workers
  ask for it.

# 5. Constraints

- One write transaction per change, using `tx.now`; the cycle check reads the
  dependency graph inside it, so a concurrent edit cannot slip a cycle in.
- No new table: `task_deps` already holds the graph.
- Same authorization on REST and MCP; workers may not edit dependencies.

# 6. Implementation Consequences

One PR:

1. This spec, its index link, and worker lifecycle 3.2 and 3.6 pointing to it.
2. `Action.DEPS_ADDED` (`ready → pending`) in the state machine.
3. The endpoint, the MCP tool, the cycle check, and the inbox rows.
4. Tests: replace and state changes in each allowed state, refusal in the
   others, stale version, unknown ids, self and transitive cycles, the cancelled
   dependency released by removal, inbox rows, totals and the `pending` filter,
   and MCP forwarding.
