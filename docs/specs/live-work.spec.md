# Live work

# 1. Purpose and Scope

A person watching the dashboard should see what an agent is doing while it does
it, not only when its task changes state. This spec adds two views built on data
Argus already has:

1. **Task activity.** The task drawer opens with the current attempt's progress:
   the latest note and percent, how long ago it came, and the notes of this
   attempt as a feed. It updates from the live event stream.
2. **Agent drawer.** Opened from the Agents tab or from a task's assignee, it shows
   the agent (state, heartbeat, runtime, capabilities, instance), the task it
   holds with the same activity, and that task's live log (NanoClaw/delegate
   stream). When it holds no task but runs on a pool instance, the container
   output tail is shown instead.

The sources are:

- Progress notes (`task.progress` events, [worker lifecycle](worker-lifecycle.spec.md)),
  at most one per task every 10 s. They are the agent's own account of its work;
  NanoClaw sends the agent's plain replies as notes.
- `GET /api/instances/{id}/logs`, read live from the target and never stored
  ([MCP operations](mcp-operations.spec.md) 3.3).
- A new `assignee` filter on `GET /api/tasks` (and MCP `list_tasks`) to find the
  task an agent holds.

# 2. Non-Goals

- No transcript or tool-call stream from the agent into Argus, and nothing new
  stored: no table, no log capture, no upload path. A full transcript, if ever
  wanted, is a task artifact uploaded at the end, in its own spec.
- No change to the progress rate limit.
- No log streaming (follow) endpoint: the drawer re-reads the tail.
- No logs for agents without an instance (workers started outside a pool); the
  drawer says so.
- No change to what NanoClaw logs. At its default level the worker container shows
  NanoClaw's lifecycle lines (claims, sessions, warnings), not the agent's own
  output.

# 3. Decisions Made

## 3.1 Task activity

- Shown at the top of the task drawer while the task is `claimed`, and for any
  task whose current attempt sent a note.
- The header is the latest `progress_message`, a bar for `progress_percent`, and
  "updated N s ago" counted on a one-second clock, so a silent agent is visible as
  one whose last note keeps ageing. The lease expiry is next to it.
- The feed lists the `task.progress` events of the current attempt, newest first,
  from the events the drawer already reads (the latest 50 for the task); the
  stream refetches them when a new one arrives.

## 3.2 Agent drawer

- Opens from the agent's name on the Agents tab and from the assignee in the task
  drawer (it stacks over it).
- Facts: role, state, last heartbeat (on the one-second clock), runtime
  (`worker_version`, `engine`, `executor`, as reported), capabilities, instance.
  The Drain action stays on the table.
- Current work: `GET /api/tasks?assignee={id}&state=claimed`, each with its title
  and the activity of 3.1.
- Live stream: when the agent holds a claimed task, the same **Live** panel as
  the task view ([task live log](task-live-log.spec.md) 3.6) for that attempt —
  that is the NanoClaw/delegate output, not container stdout.
- Container output (fallback): when the agent holds no task but has an
  `instance_id`, the last 200 lines of `GET /api/instances/{id}/logs`, re-read
  every few seconds while the drawer/panel is open and not paused, kept scrolled
  to the end unless the reader scrolled up. A failure (target unreachable, not
  permitted) shows its message in place and keeps retrying. Closing stops the
  reads. At NanoClaw's default level this is lifecycle lines only (claims,
  sessions, warnings), not the agent's own stream.

## 3.3 `assignee` filter

- `GET /api/tasks?assignee={agent_id}` lists the tasks that agent holds now. An
  assignee exists only while a task is `claimed`, so earlier attempts are not
  included. It combines with the other filters and is bound into the cursor like
  them. MCP `list_tasks` forwards it.

# 4. Open Questions

- Whether NanoClaw should send a progress note when the agent starts a tool or a
  delegated executor, so the feed shows activity between replies. Decide after
  watching real runs with this view.
- Whether the log panel should offer a larger tail or a download.

# 5. Constraints

- Dashboard talks to the API only. No new write path, table or migration.
- Overhead only while someone watches: the stream already exists, and log reads
  happen only with an agent drawer open (one target call per 5 s per open drawer).
- Logs keep the existing scope (managers and admins); the drawer does not widen it.

# 6. Implementation Consequences

One PR:

1. This spec and its index link.
2. `assignee` on the task list (REST, MCP) with a test.
3. Dashboard: a shared activity component, the task drawer using it, the agent
   drawer (facts, current work, log panel), and the entry points. Tests for the
   activity feed (current attempt only), the agent drawer's work and log polling
   (stops when closed), and the missing-instance message.
