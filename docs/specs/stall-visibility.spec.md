# Stall visibility

# 1. Purpose and Scope

Operators cannot tell a busy worker from a dead one. A claimed task whose session
container has exited, or that has gone silent for hours, keeps its lease alive
through heartbeats and never appears in the inbox. This spec makes those stalls
visible and explainable:

1. The NanoClaw channel fails an attempt when its session container vanishes
   without a reply (`session_lost`).
2. Argus flags a claimed task that has had no signal for 20 minutes as quiet
   (inbox row, worker rail, `task.quiet` event). Quiet never expires the task.
3. Lease expiry and agent loss record the last signal and how late they were
   detected when Argus was not running.
4. A claim that was flagged quiet and then stays silent for `ARGUS_STALL_FAIL_S`
   (default 3600 s) is expired as `stalled` and retried, instead of holding the
   task until the wall-clock timeout.
5. Each heartbeat says what the host sees for the claim (workspace, session,
   delegate), so quiet, stalled and expired claims name a likely cause, and a
   workspace that is gone blocks the attempt instead of spending it.

# 2. Non-Goals

- No absolute session ceiling: NanoClaw's stays removed
  ([worker lifecycle](worker-lifecycle.spec.md) 3.8). Only silence ends an attempt
  (3.6), never how long it has run.
- No change to lease TTL, heartbeat interval, attempt timeout, or `lost` rules.
- Final `failed` tasks stay out of the inbox; Fleet owns failure totals.
- No new service, broker, or table beyond nullable columns on `tasks`.
- The stall cause (3.7) is descriptive: it never moves the quiet or stall
  thresholds and never decides an expiry or requeue.

# 3. Decisions Made

## 3.1 Session-lost fail-fast (channel)

While a claimed task is injected (`sessionCarriesTask`), each heartbeat tick
lists the group's session containers. Once a container has been seen, if none
remain for 90 s and no reply has arrived, and the channel is not mid-recycle or
mid-resend, the channel:

- inspects the last known container for its exit code and takes the last 40 log
  lines (at most 2 KiB, redacted like the live log);
- `POST /tasks/{id}/fail` with `reason: "session_lost"` and an error naming the
  container, exit code and a short log excerpt;
- finishes the task (same cleanup as any other fail).

A quiet container that is still running is never failed this way.

## 3.2 Activity and quiet (control plane)

- Each heartbeat lease may carry optional `activity_at` (ISO UTC). The channel
  sets it to the newest of: the NanoClaw session heartbeat file mtime, the
  delegate log relay's last line, the last progress it sent. The server clamps
  it to `<= tx.now` and never moves it backwards. A newer `activity_at`, a
  progress post, or a live-log post clears `quiet_at`.
- Columns `tasks.activity_at` and `tasks.quiet_at` (nullable). Migration `0045`.
- Reaper stage (isolated savepoint, every pass): for each `claimed` task with
  `quiet_at IS NULL`, last signal =
  `max(activity_at, progress_at, last live-log at, claim time)`. When that is
  older than `ARGUS_QUIET_CLAIM_S` (default 1200), set `quiet_at = tx.now` and
  emit `task.quiet` with `{attempt, agent_id, last_signal_at,
  last_signal_source}`. The stage never expires or requeues.
- Clearing: a newer signal clears `quiet_at` only; the next silence can flag
  again.

## 3.3 Explained expiry and loss

- `_expiry_error` appends the last signal and its source when known.
- When `tx.now - deadline` exceeds three reaper intervals (30 s),
  `task.expire` / `agent.lost` data include `detected_late_s`, and the error
  text says `detected N min late (Argus was not running)`.
- Fleet `classify_failure` keeps `session_lost` as a first-class reason.

## 3.4 Inbox and agents

- Inbox rows also include `claimed` tasks with `quiet_at IS NOT NULL`
  (`since = quiet_at`). `InboxItem.last_signal_at` and nullable
  `last_signal_source` are the newest of activity / progress / log / claim.
- `wait_for_events(inbox=true)` includes `task.quiet`.
- Agent views expose `quiet_since`: the earliest `quiet_at` among the agent's
  claimed tasks (null when none).

## 3.5 Dashboard

- Inbox: quiet row copy "Quiet for N min; last signal: &lt;source&gt; at &lt;time&gt;",
  with the existing release and steer actions.
- Worker rail: a busy chip with `quiet_since` shows state `quiet` and its age;
  workers that ended in the last hour stay listed (dimmed) with `end_reason`.

## 3.6 Stalled claims

Production data (2026-10-16 to 2026-10-24): 13 of 15 attempts that hit the 6 h
timeout had their last progress or log line within minutes of the claim and then
nothing for hours, while heartbeats kept the lease alive.

- Reaper stage `expire_stalled`, after `expire_timeouts`: a `claimed` task with
  `quiet_at` set whose last signal (3.2) is older than `ARGUS_STALL_FAIL_S` is
  expired with reason `stalled`, like a timeout: `failures` +1, requeued until
  `max_attempts`. `task.expire` carries `last_signal_at` and `last_signal_source`.
  The worker learns of it on its next heartbeat and stops the session, as for any
  expiry. Requiring `quiet_at` means the inbox warned first.
- `ARGUS_STALL_FAIL_S` defaults to 3600 and must exceed `ARGUS_QUIET_CLAIM_S`
  to ever fire; `0` disables. Any progress post, live-log line or `activity_at`
  (delegate log relay, session heartbeat file) resets the clock, so a long
  executor run that prints is never stalled.
- Fleet `classify_failure` keeps `stalled` as a first-class reason, with its time
  held, so the effect shows in Fleet.

## 3.7 Stall causes

Incident (2026-10): hosts kept heartbeating while their instance directories had
been deleted, so sessions never started and each claim sat quiet until
`stalled` spent an attempt.

- Channel: `RuntimeHooks.workspaceState()` (optional) stats the Argus agent group
  directory on the host: a directory is `ok`, `ENOENT` is `missing`, any other
  error, or no answer within 5 s, is `unknown`. While a task is claimed, each
  heartbeat lease carries an optional `observation`:
  `{workspace: ok|missing|unknown, session: running|gone|unknown,
  delegate: active|silent|unknown}`. `session` is what the 3.1 watch saw
  (`unknown` when listing fails, before the first container, or mid-recycle).
  `delegate` is `active` when the newest `activity_at` source (3.2) is under
  5 min old, `silent` when older, `unknown` when no source answered.
- Server: each part is a closed enum (`422` otherwise); unknown keys are
  ignored. The heartbeat stores the newest observation in `tasks.observation`
  (JSON) and `tasks.observation_at` (`tx.now`, never moved backwards); expiry
  clears both. Migration `0050`.
- `stall_cause` is derived when read: no observation, one older than three
  heartbeat intervals (90 s), or one sent before the current claim, is
  `unknown`. Otherwise, in this order: workspace `missing` is
  `workspace_missing`; session `gone` is `session_gone`; delegate `active` is
  `silent_delegate_active`; `ok`/`running`/`silent` is `no_progress`; anything
  else is `unknown`. `task.quiet` and `task.expire` carry `stall_cause` and
  `observation_at`; inbox quiet rows carry `stall_cause`; a `stalled` expiry
  error, or any expiry with a known cause, ends with `stall cause: <cause>`.
- `env_workspace_missing` (class `environment`, [failure classes](failure-classes.spec.md)
  3.3): when the channel sees `workspace: missing` for 90 s (the 3.1 grace) while
  the task is injected and no recycle or resend is in progress, it blocks the
  attempt with that reason and an error naming the probed directory (not its
  path). No attempt is spent; the continuation and WIP are kept; the task shows
  in the inbox. It never stops or recycles a session, and never acts while the
  delegate is `active`.
- `unknown` is conservative: it never triggers an action, and a running grace
  is neither reset nor fired by it (only `ok` resets it). A probe that flaps
  between `missing` and `unknown` therefore still blocks after 90 s of not being
  `ok`, while one that cannot tell at all never blocks.
- Session death keeps the 3.1 `session_lost` path; a busy session with an active
  delegate is never touched.

# 4. Open Questions

- Should a final `failed` task with `reason=session_lost` also land in the inbox
  for a short window, or is Fleet enough?

# 5. Constraints

- Single-node, SQLite only; reaper stages stay independent savepoints.
- English only; no credentials in fail errors or log excerpts.
- Channel docker inspect/logs are best-effort; a probe that fails still fails
  the attempt with a short "session container gone" error.

# 6. Implementation Consequences

- Spec + index; amendments to [worker lifecycle](worker-lifecycle.spec.md) 3.8
  and [MCP operations](mcp-operations.spec.md) 3.2.
- Migration `0045` (3.2) and `0050` (3.7); heartbeat, reaper, expiry, inbox, agent views, fleet
  classifier; `make openapi`.
- NanoClaw channel watchdog + `activity_at` on heartbeat; dashboard Inbox and
  WorkersSidebar; tests for each layer.
- 3.7: `LeaseObservation` on the heartbeat, `stall_cause` in events, inbox and
  errors, `env_workspace_missing`, the channel workspace probe and its test
  (`argus-stall-causes.test.ts`).
