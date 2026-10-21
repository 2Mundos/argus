# Task live log

# 1. Purpose and Scope

A claimed task shows a progress note and a milestone, but not what the executor is
printing now. This spec adds a **live log**: a bounded, redacted tail of an attempt's
output that the worker side posts, the API keeps and streams, and the dashboard
("Ao vivo" in the task view) and MCP read. It also makes output count as a sign of
life for the no-signal indicator of [progress milestones](progress-milestones.spec.md).

# 2. Non-Goals

- Not a full transcript, an archive or a search index: a ring of recent lines.
- Not a replacement for the artifacts a worker uploads or for `get_instance_logs`
  (container output of a pool instance).
- No write tool over MCP; the producers are the NanoClaw channel and
  `argus-delegate`. The delegate has no token: it writes to a relay file and the
  NanoClaw channel, which holds the token, posts it (3.7).
- Redaction is best effort; it does not make it safe to print secrets.

# 3. Decisions Made

## 3.1 Writing: `POST /api/tasks/{id}/log`

- Agent token only (worker or orchestrator); admin tokens are refused like on
  `/progress`. Body: `{attempt: int, source: "worker" | "delegate", lines: [str]}`.
- **Fencing is the progress endpoint's**: the caller must be the assignee of a
  `claimed` task, its agent live, the lease unexpired, and `attempt` the task's
  current one. Otherwise `409` with `not_claimed`, `not_assignee`, `stale_attempt`,
  `lease_expired` or `agent_<state>`. An old attempt can never write into a newer
  one, and a refused batch writes nothing. Unlike progress, a log batch does
  **not** renew the lease; heartbeats and progress do.
- **Limits** (all enforced server-side):

  | Limit | Value | Over the limit |
  | --- | --- | --- |
  | lines per batch | 200 (and at least 1) | `422 validation_failed` |
  | characters per line | 2000, after redaction | cut to 1999 characters plus `…` |
  | batch size | 64 KiB of UTF-8 as sent | `413 log_batch_too_large` |
  | rate | one batch per second per task and source | `429 rate_limited`, with the wait |

  A line is cut, not refused, because output the client cannot shorten is better
  shown cut than lost. A batch every 2-5 s is well inside the rate.
- **Cleaning**, in this order, on the whole batch: redaction (3.2); split on newlines;
  terminal escape sequences and control characters removed; trailing whitespace
  stripped; blank lines dropped; long lines cut. A batch that is blank afterwards is
  accepted with `accepted: 0` and changes nothing.
- Answer: `{accepted, last_seq, output_at}`.

## 3.2 Redaction

One pure function, `argus_core.redact.redact(text, secrets=())`, runs before anything
is stored or published. It masks, with `[redacted]`: the caller's own bearer token
(passed as an exact secret), `Authorization`/`Proxy-Authorization` headers and
`Bearer` tokens, JWTs, `sk-` keys, GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
`github_pat_`), GitLab (`glpat-`), Slack, AWS (`AKIA`/`ASIA`) and Google (`AIza`) keys,
the password in `scheme://user:password@host`, assignments to names ending in
`token`, `secret`, `password`, `passwd`, `pwd`, `api_key`, `access_key`,
`private_key` or `credential(s)` (`NAME=value`, `name: value`, JSON `"name": "value"`),
`--token value` style flags, and PEM private key blocks. A private key block is
caught only within one batch (an unterminated one is masked to the end of the batch);
clients should send a block in one batch.

## 3.3 Storage and retention

- Table `task_log(task_id, attempt, seq, source, at, line)`, primary key
  `(task_id, attempt, seq)`. `seq` counts lines per task, across attempts, from
  `tasks.log_seq` (never reused). Every line of a batch has the batch's `at`.
- **Ring:** an attempt keeps its newest **2000** lines; each write deletes the older
  ones in the same transaction.
- **Attempts:** only the current attempt and the two before it keep lines (older
  attempts' rows are deleted on write). `retry` removes the whole log, because
  `attempt` restarts at 1.
- `tasks.output_at` is the time of the last accepted batch. It is in the task views
  (`output_at`, null until the first batch of the attempt). A claim and a retry
  clear it.

## 3.4 Live: SSE `task.log`

- Each accepted batch is published on the in-process bus after commit as a **transient
  event**: kind `task.log`, subject `task` / the task id, actor `agent:<id>`, data
  `{task_id, attempt, source, seq, at, lines}` where `lines` are the stored (redacted)
  strings and line `i` has sequence `seq + i`.
- A transient event is **not** in the `events` table, has `id` 0, and is sent on
  `/api/events/stream` without an `id:` line, so it never changes `Last-Event-ID` and
  is never replayed. A client that reconnects backfills with 3.5.
- Workers receive it only for tasks they have leased, by the same check as other task
  events. It is not returned by `GET /events` or `/events/wait`.

## 3.5 Reading: `GET /api/tasks/{id}/log?attempt=&after=&limit=`

- `attempt` defaults to the task's current one. `after` is a `seq`: the lines after it,
  oldest first. Without it, the newest `limit` lines (default 200, max 1000), oldest
  first. Answer `{attempt, lines: [{seq, source, at, line}], last_seq}`; `last_seq` is
  the last returned line's, else `after` (0 when not given). To follow, pass
  `last_seq` as `after`.
- Read scope is the task's: managers read all, workers only tasks they have leased
  (`403 not_leased`).
- MCP: `get_task_log(task_id, attempt?, after?, limit?)`, listed to every scope, same
  permissions as REST.

## 3.6 Dashboard

- Panel **Ao vivo** in the task view: backfill with 3.5, then follow `task.log` through
  the shared event stream for that task, de-duplicated by `seq`. It shows the attempt,
  filters `todos` / `worker` / `delegate`, scrolls to the end while the reader is at the
  end, and has pause/resume (paused lines are buffered and shown on resume).
- **No signal** uses the later of `progress_at` and `output_at`; the threshold is
  unchanged (`NO_SIGNAL_MINUTES`, `VITE_NO_SIGNAL_MINUTES`).

## 3.7 Delegate client

- `argus-delegate` runs claude with `--output-format stream-json --verbose` and follows
  its own stdout file. Events become short lines (assistant text, `▸ Tool arg`,
  truncated tool results, errors), redacted best effort with the 3.2 patterns, and are
  batched every ~3 s. After ~30 s of silence it adds `delegate alive, Ns since last
  output`, so No signal reflects a live process.
- The batches are appended as JSON lines `{v, task_id, attempt, at, lines}` to
  `argus-task-log.jsonl` in the agent group directory (beside the model-policy
  snapshot, no new mount). The file is capped at 8 MiB per run; a relay failure is
  silent and never fails or slows the run. The token is never in that environment.
- The channel (`integrations/nanoclaw/argus.ts`) empties the file at task start, polls
  it every 2 s, keeps only records of its current task and attempt, and posts one batch
  per poll with `source: "delegate"` (<= 200 lines, 60 KiB). `409` `stale_attempt`,
  `not_assignee`, `not_claimed` or `401` stop the relay; other `409`, `413`, `422` drop
  the batch; `429`, `5xx` and network errors back off (2 s doubling to 30 s) and retry.
  A last poll runs before COMPLETE/FAIL/ASK.

# 4. Open Questions

- Only the `claude` executor is parsed into readable lines; other executors give
  keepalive lines only.
- A per-batch rate limit of one second may be too tight for a very chatty executor;
  the client batches, so it is not expected to matter.

# 5. Constraints

- Single-node: SQLite and the in-process bus only. A write is one local transaction
  with no network call; time is `tx.now`.
- A log line is untrusted output: the dashboard renders it as text, never as HTML.
- Migration `0028` is additive: two nullable or defaulted task columns and one table.

# 6. Implementation Consequences

One PR for the API side: this spec and its index link; migration `0028`;
`argus_core.redact`; `tasks/log.py`, the two routes, the transient event path in
`events.py`, `Database.write` and the SSE stream; `output_at` in the task views; MCP
`get_task_log` and the catalog in [MCP operations](mcp-operations.spec.md); the
dashboard panel and no-signal change; regenerated `openapi.json` and `schema.d.ts`;
tests for each. The `argus-delegate`/NanoClaw client is a separate change.
