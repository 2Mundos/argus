---
status: draft
created: 2026-11-05
updated: 2026-11-05
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
  stub node with that task's title and state, not expanded.
- No saved layout: positions are computed on every render.

# 3. Decisions Made

## 3.1 API

- `GET /api/projects/{name}/task-graph` (any dashboard caller; workers only see
  tasks they leased, as on the task list) → MCP `get_task_graph`. Both call one
  service function.
- Response: `nodes` (id, title, state, kind, priority, block_reason,
  `failed_dependencies`) and `edges` (`{from: depends_on_id, to: task_id}`),
  read in one snapshot. `truncated: true` when the cap is hit.
- Nodes are the project's non-archived tasks, at most 500, chosen newest first
  and then closed over their dependencies inside the project, so no edge points
  at a missing node. Tasks `succeeded` more than 7 days ago with no unfinished
  dependent are left out unless `include_done=true`.

## 3.2 Dashboard

- A **Graph** tab on the project page, beside the task list
  ([dashboard navigation](dashboard-navigation.spec.md)).
- Layered left-to-right layout: a task's column is the length of its longest
  dependency chain. Nodes are colored by state; a `pending` node with a failed
  or cancelled dependency is marked stuck, matching the inbox (task
  dependencies 3.2).
- **Ready frontier**: `ready` nodes are highlighted, as the next work to run.
- Clicking a node opens task detail; hovering one highlights its ancestors and
  descendants.
- It refreshes on the task live events the dashboard already receives; no new
  event type.

# 4. Open Questions

- Layout library: a small in-house layering (longest path plus barycenter
  ordering) versus `dagre` or `elkjs` with React Flow. Pick the smallest that
  keeps crossings readable at 500 nodes.
- Whether to show the critical path. Without durations it is the longest
  unfinished chain by count; decide once operators ask for it.
- Whether subtasks (`parent_id`) are grouped into their parent's box.

# 5. Constraints

- Read-only: one snapshot, no write transaction, no new table or migration.
- Bounded response (500 nodes) so a large project cannot stall the API.
- The dashboard reads the graph through the API only.

# 6. Implementation Consequences

One PR:

1. This spec moves to `implemented`; task dependencies links to it.
2. The service function, REST route, MCP tool, and `make openapi`.
3. The Graph tab and its layout.
4. Tests: closure over dependencies, cross-project stubs, the 500-node cap and
   `truncated`, the done-task cutoff and `include_done`, worker scope, and MCP
   forwarding. Layout is checked with a screenshot, not a test.
