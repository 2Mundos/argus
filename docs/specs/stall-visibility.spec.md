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

# 2. Non-Goals

- No automatic kill of a quiet-but-alive session (NanoClaw's absolute ceiling
  stays removed; [worker lifecycle](worker-lifecycle.spec.md) 3.8).
- No change to lease TTL, heartbeat interval, attempt timeout, or `lost` rules.
- Final `failed` tasks stay out of the inbox; Fleet owns failure totals.
- No new service, broker, or table beyond two nullable columns on `tasks`.

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
- Migration `0045`; heartbeat, reaper, expiry, inbox, agent views, fleet
  classifier; `make openapi`.
- NanoClaw channel watchdog + `activity_at` on heartbeat; dashboard Inbox and
  WorkersSidebar; tests for each layer.
