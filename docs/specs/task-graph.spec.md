---
status: implemented
created: 2026-11-05
updated: 2026-11-11
---

# Task graph

# 1. Purpose and Scope

A project's tasks and their [dependencies](task-dependencies.spec.md) form a
directed acyclic graph. Today it can only be read one task at a time (the
`depends_on` list on task detail). This spec adds a **graph view** on a
project: tasks as nodes, dependencies as arrows, laid out left to right in
layers, so an operator sees at a glance what is ready, what is running, and
which chains are blocked and by what.

# 2. Non-Goals

- No time axis (that would be a Gantt chart) and no durations or estimates.
- No editing by dragging edges in this version; dependencies are still edited
  through task edit (`PATCH /api/tasks/{id}`).
- No graph across projects. An edge to a task in another project is drawn as a
  stub node the caller may read, not expanded (3.1).
- No saved layout: positions are computed on every render.

# 3. Decisions Made

## 3.1 API

- `GET /api/projects/{name}/task-graph?include_done=` (any dashboard caller;
  workers only see tasks they leased, as on the task list; `404` for an unknown
  project, `403 project_scope` for a worker with no lease in it) → MCP
  `get_task_graph(project, include_done?)`, listed to orchestrators and admins.
  Both call one service function (`tasks/graph.py`).
- Response: `nodes` (id, title, state, kind, priority, block_reason,
  `failed_dependencies`, `project`) and `edges` (`{from: depends_on_id, to:
  task_id}`), read in one snapshot. `failed_dependencies` lists a `pending`
  node's `failed` or `cancelled` dependencies (as in the inbox); other nodes
  have none. Nodes are ordered by `created_at`, then id; edges by `from`, then
  `to`.
- Selection: the project's non-archived tasks, newest first, each added with its
  dependencies inside the project (transitively), so no edge points at a missing
  node. At most 500 nodes. The first task whose dependencies do not fit in what
  is left ends the selection: it and every older task are left out, and
  `truncated` is `true`. `truncated` is `true` exactly when some selected
  candidate was left out, and `false` otherwise.
- Done cutoff: tasks `succeeded` with `updated_at` more than 7 days ago and no
  unfinished (not `succeeded`, not archived) dependent are left out unless
  `include_done=true`; they still appear when a shown task depends on them.
- Archived dependencies are not drawn: the edge is dropped, as for a dependency
  the caller may not see.
- Scope applies before anything is selected. For a worker only leased tasks are
  candidates, dependencies and dependents it did not lease are ignored (not
  named in `failed_dependencies`, not counted by the done cutoff), and their
  edges are dropped.
- A dependency in another project is a **stub node** (it counts toward the 500
  and is not expanded): `project` names its project, `failed_dependencies` is
  empty and `block_reason` is `null`. A foreign task the caller may not read is
  not emitted at all, with no edge, so its id, title and state never leave the
  API.

## 3.2 Dashboard

- A **Graph** tab on the project page (`#projects/<slug>/graph`, beside Overview
  and Task defaults; [dashboard navigation](dashboard-navigation.spec.md)), with
  a "show tasks finished over 7 days ago" toggle (`include_done`).
- Layered left-to-right layout, in house (no layout library): a task's column is
  the length of its longest dependency chain; rows follow the barycenter of the
  neighbouring column, three sweeps. Nodes are colored by state; a `pending`
  node with a failed or cancelled dependency is marked stuck, matching the inbox
  (task dependencies 3.2); a stub of another project has a dashed border.
- **Ready frontier**: `ready` nodes are outlined, as the next work to run.
- Clicking a node opens task detail (`#tasks/<id>`); hovering or focusing one
  highlights its ancestors and descendants and dims the rest.
- It refreshes on the task live events the dashboard already receives
  (`useLiveData`); no new event type, no polling. A `truncated` graph says so.

# 4. Open Questions

- Whether to show the critical path. Without durations it is the longest
  unfinished chain by count; decide once operators ask for it.
- Whether subtasks (`parent_id`) are grouped into their parent's box.

# 5. Constraints

- Read-only: one snapshot, no write transaction, no new table or migration.
- Bounded response (500 nodes) so a large project cannot stall the API.
- The dashboard reads the graph through the API only.

# 6. Implementation Consequences

Built in one PR:

1. This spec is `implemented`; task dependencies links to it.
2. `tasks/graph.py` (service function and route), the MCP tool, and `make
   openapi`.
3. The Graph tab and its layering helper (`taskGraph.ts`).
4. Tests: closure over dependencies, the 500-node cap and `truncated`, no
   dangling edge, ordering, the done-task cutoff, `include_done` and archive,
   cross-project stubs without leaks, worker scope, MCP forwarding, and the
   layering helper. The look is checked with a screenshot.
