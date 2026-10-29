# Task changes

# 1. Purpose and Scope

While a `repository:write` task runs, the dashboard shows what the agent says
(progress, milestones) and what it prints (the [live log](task-live-log.spec.md)),
but not what it has changed. The code is visible only once a pull request exists
([task delivery](task-delivery.spec.md)), often after the attempt is over. A manager
who steers a task ([task steering](task-steering.spec.md)) cannot see whether the
work actually changed direction, and a reviewer of a blocked or failed attempt
cannot see the work in progress without opening the instance.

This spec adds **task changes**: a bounded, redacted snapshot of the task
workspace's diff against its base. The worker side computes it inside the session
container, the API keeps the latest snapshot per attempt, and the dashboard (a
**Changes** panel beside Live) and MCP read it.

In scope:

- The snapshot format and how it is computed in the container.
- `PUT /api/tasks/{id}/changes`, storage, the transient `task.changes` event and
  `GET /api/tasks/{id}/changes`.
- The NanoClaw relay, the dashboard panel and an MCP read tool.

Related: [task workspace](task-workspace.spec.md) (the path and branch), [task live
log](task-live-log.spec.md) (whose fencing, redaction and relay this reuses), [worker
read scope](worker-read-scope.spec.md).

# 2. Non-Goals

- **No git on the host.** The host never runs `git diff`, `git status` or any other
  command that reads a worktree in a task workspace. The worker controls `.git`
  (config, attributes, filters, textconv drivers), and those run commands. Task
  workspace 3.1's hardening covers `init` and `remote add`, not reads (3.2).
- **Not a review tool.** No comments, no approve or reject, no merge. Review stays on
  the forge.
- **No history.** One snapshot per attempt, replaced on each write. The pull request
  and the branch are the history.
- **No full repository.** The patch is bounded (3.1); a large change shows its
  file list and a truncated patch.
- **No syntax highlighting** and no new dashboard dependency.
- Tasks without `repository:write` have no workspace and no changes.
- Redaction is best effort, as for the live log.

# 3. Decisions Made

## 3.1 The snapshot

```json
{
  "v": 1,
  "task_id": "…", "attempt": 2, "at": "2026-10-29T12:00:00Z",
  "branch": "argus/task-1a2b3c4d",
  "base": {"ref": "origin/main", "sha": "…"},
  "head": "…",
  "commits": [{"sha": "…", "subject": "…"}],
  "files": [{"path": "…", "status": "M", "added": 12, "deleted": 3, "binary": false}],
  "untracked": [{"path": "…", "bytes": 812}],
  "patch": "diff --git …",
  "truncated": {"files": false, "patch": true}
}
```

- **Base.** `git merge-base HEAD origin/<default>` when the remote's default branch
  has been fetched, else the empty tree (`base: null`): an unborn branch with no
  fetch is all additions.
- **Diff.** The worktree, staged and unstaged, against the base:
  `git diff --no-ext-diff --no-textconv --no-color -M <base>`, with `--numstat`
  and `--name-status` for `files`. Untracked, non-ignored files are listed with their
  size only (`git ls-files --others --exclude-standard`, `lstat`, never following a
  link); their contents are not in the patch, since that would mean writing to the
  index (`add -N`). The delegate's own output directory (`.argus-delegate/`) is left
  out of both.
- **Commits.** `git log --format=%H%x00%s <base>..HEAD`, newest first, at most 50.
- **Bounds.** At most 500 `files` and 200 `untracked`; the patch at most 256 KiB, and
  each file's hunk at most 64 KiB, cut at a line with a `… (cut)` marker. A cut
  sets the matching `truncated` flag. A file git calls binary shows only in
  `files`.
- **Redaction.** The producer runs the live log's patterns (task live log 3.2) over
  `patch`, commit subjects and paths; the API runs `argus_core.redact.redact` again
  on every string before storing (3.3). An added line that matches a secret pattern
  is still shown, masked. That is the point: a committed secret is a finding.

## 3.2 Producer: inside the container

- `engines/changes.mjs`, started by `argus-delegate` with each executor attempt: a
  snapshot every **30 s** while the executor runs (`ARGUS_CHANGES_TICK_S`), only when
  HEAD or the `git status --porcelain` output changed since the last one, and always
  one when the executor ends. Git runs in the session
  container, as the worker's own user, on the worker's own workspace. What the
  worker's `.git` can make git do, it could already do in that container.
- Every git call runs with `GIT_OPTIONAL_LOCKS=0` and `-c core.fsmonitor=false`,
  and a 10 s timeout. A failure or timeout skips the snapshot and is never an error
  of the run.
- The snapshot is written whole to `argus-task-changes.json` in the agent group
  directory (beside `argus-task-log.jsonl`), by write-to-temp then rename. The token
  is never in that environment.
- The channel (`integrations/nanoclaw/argus-changes-relay.ts`, started by
  `argus.ts` with the live log relay) reads the file with the live log's rules (task
  live log 3.7): `O_NOFOLLOW`, a regular file with one link, at most 1 MiB, removed
  and recreated at task start. It checks `v`, `task_id` and `attempt` against the
  claimed task, rebuilds the body field by field (the file is untrusted), and puts it
  when its `at` differs from the last one put, at most once per 10 s. Before
  COMPLETE/FAIL/ASK it drains: the last snapshot is put as soon as the server's 5 s
  pace allows, within 6 s. `409` `stale_attempt`, `not_assignee`, `not_claimed` and
  `401` stop the relay; another `4xx` drops that snapshot; `429`, `5xx` and network
  errors back off (2 s doubling to 30 s) and retry it.

## 3.3 Writing: `PUT /api/tasks/{id}/changes`

- Agent token only. Body: the snapshot of 3.1, minus `task_id`, plus `attempt`.
- **Fencing is the live log's** (task live log 3.1): assignee of a `claimed` task,
  agent live, lease unexpired, current `attempt`; otherwise `409` with the same
  codes. It does not renew the lease.
- **Limits:** body at most 384 KiB serialized (`413 changes_too_large`); one write
  per 5 s per task (`429 rate_limited`, with the wait), so the final drain is never
  refused after a periodic put; the bounds of 3.1 are checked (`422
  validation_failed`) and the patch cut again if needed.
- The write replaces the attempt's snapshot. Answer `{at, files, added, deleted}`.

## 3.4 Storage

- Table `task_changes(task_id, attempt, at, received_at, branch, base_ref, base_sha,
  head, commits, files, untracked, patch, truncated)`, primary key
  `(task_id, attempt)`; JSON columns as text. `received_at` is `tx.now`.
- Like the live log, only the current attempt and the two before it keep a row;
  older rows are deleted on write. `retry` removes them all.
- Migration `0052_task_changes.sql`, additive.

## 3.5 Live: SSE `task.changes`

- After commit, a **transient** event (task live log 3.4): kind `task.changes`,
  data `{task_id, attempt, at, head, files, added, deleted}`, the **summary only**,
  never the patch. Same delivery and worker visibility rules as `task.log`.

## 3.6 Reading: `GET /api/tasks/{id}/changes?attempt=`

- `attempt` defaults to the current one. Answer the stored snapshot, or `404
  no_changes` when the attempt has none. Read scope is the task's (managers all,
  workers only tasks they have leased).
- MCP: `get_task_changes(task_id, attempt?)` with the same permissions, added to the
  [MCP operations](mcp-operations.spec.md) catalog. Orchestrators can then check a
  worker's direction without reading its instance.

## 3.7 Dashboard

- Panel **Changes** below **Live**: in the task view for a `repository:write` task,
  and in the agent drawer once the held attempt has a snapshot (the task summary
  there does not carry capabilities). A header with the branch, `head` (short), the
  snapshot age and the counts. Then the base, the commits, the file list (status,
  `+added −deleted`), and the patch per file, collapsed beyond the first five, with
  added/removed lines coloured.
- It loads with 3.6 and refreshes on `task.changes` for that task and attempt. A
  "cut" banner shows when any `truncated` flag is set; untracked files list with
  "not in the diff".
- An attempt selector shows the previous attempts' last snapshots, so the work an
  expired or failed attempt left is visible.
- Everything is rendered as text, never HTML. Paths are not links.

# 4. Open Questions

- **Agents that do not delegate.** Snapshots come from `argus-delegate`. An engine
  that edits the workspace itself (no executor) produces none until a delegate runs.
  A harness hook like `overlays/harness-usage.ts` could call the same producer after
  each harness turn.
- **Untracked contents.** Showing new files' contents needs either `add -N` (writes
  the index the worker uses) or reading files directly, which is a second redaction
  path. Left out until it is missed.
- **Base when the default branch moves.** A fetch moves the merge base, so two
  snapshots may differ for reasons outside the task. Pinning the base at the first
  snapshot of the task is the alternative.

# 5. Constraints

- Single node: SQLite and the in-process bus. A write is one local transaction with
  no network call; time is `tx.now`.
- The host runs no git over a task workspace (2). The relay file is untrusted input.
- Diff text is untrusted output: rendered as text, redacted twice (3.1).
- Storage is bounded: 384 KiB per row, three rows per task.

# 6. Implementation Consequences

- API: this spec and its index link; migration `0052`; `tasks/changes.py`, the two
  routes, the transient event; MCP `get_task_changes` and its catalog entry;
  regenerated `openapi.json` and `schema.d.ts`; tests for fencing, limits,
  redaction, retention.
- Worker: `engines/changes.mjs` and its call from `argus-delegate.mjs`, shipped in the
  engines and tools layers; `argus-changes-relay.ts` and its wiring in `argus.ts`,
  installed by `install.sh`; tests with a workspace that sets a malicious
  `diff.external`, `textconv`, `core.pager` and `core.fsmonitor`, proving none of
  them runs.
- Dashboard: `views/TaskChanges.tsx` and pure helpers in `taskChanges.ts`; the panel
  below Live; `task.changes` triggers no list refetch and is not shown in Events.
- Negentropy: one table, one event, two routes, one producer module. It reuses the
  live log's fencing, redaction, transient-event and relay patterns rather than
  adding a channel.
