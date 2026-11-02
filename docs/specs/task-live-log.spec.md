# Task live log

# 1. Purpose and Scope

A claimed task shows a progress note and a milestone, but not what the executor is
printing now. This spec adds a **live log**: a bounded, redacted tail of an attempt's
output that the worker side posts, the API keeps and streams, and the dashboard
(the **Live** panel in the task view) and MCP read. It also makes output count as a sign of
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
  one, and a refused batch writes nothing. The lease and rate checks run first on a
  read snapshot, so a refused batch costs no cleaning, and again in the write
  transaction, where they are authoritative. Unlike progress, a log batch does
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
- **Cleaning**, in this order, before the write transaction: `\r\n` becomes a
  newline; terminal escape sequences and control characters other than `\r` removed
  (so an escape cannot split a secret); private key blocks masked across the batch;
  each line or `\r` frame cut to 8 KiB; a line's bare `\r`s dropped when that leaves less unmasked
  (a `\r` split a secret mid-token); redaction (3.2); then a bare `\r` splits the line
  too (a progress bar's frames are separate lines), so a `\r` used as a space cannot
  split a secret either; trailing whitespace stripped; blank lines dropped; long lines
  cut; each line redacted again (a cut never unmasks). A batch that is blank afterwards is accepted
  with `accepted: 0` and changes nothing.
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
clients should send a block in one batch. Redaction stays linear in the input: a rule
starts only where its run starts (a JWT or a flag not inside a longer run of its
characters; a name by its ending, whatever its length), so no two starts rescan one
run. URL schemes are bounded to 16 characters; names, URL users and passwords are not.

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
  events; a stream remembers a task once found leased (grants are never deleted), so a
  busy log costs one read per task. It is not returned by `GET /events` or
  `/events/wait`, and never moves their cursor.

## 3.5 Reading: `GET /api/tasks/{id}/log?attempt=&after=&limit=`

- `attempt` defaults to the task's current one. `after` is a `seq`: the lines after it,
  oldest first. Without it, the newest `limit` lines (default 200, max 1000), oldest
  first. Answer `{attempt, lines: [{seq, source, at, line}], last_seq}`; `last_seq` is
  the last returned line's, else `after`; an empty tail (no `after`) gives the task's
  `log_seq`, so the attempt's next line (`log_seq + 1`, as `seq` counts across attempts)
  follows without a gap. To follow, pass `last_seq` as `after`.
- Read scope is the task's: managers read all, workers only tasks they have leased
  (`403 not_leased`).
- MCP: `get_task_log(task_id, attempt?, after?, limit?)`, listed to every scope, same
  permissions as REST.

## 3.6 Dashboard

- Panel **Live** in the task view and in the agent drawer/panel while that agent
  holds the attempt: backfill with 3.5, then follow `task.log` through the shared
  event stream for that task, de-duplicated by `seq`. It shows the attempt, filters
  All / worker / delegate, scrolls to the end while the reader is at the end,
  and has pause/resume (paused lines are buffered and shown on resume). When the
  stream reconnects, or a batch's `seq` skips past the newest line held (or past the
  empty tail's `last_seq` when none is held), it reads the missed lines with `after`.
- **No signal** uses the later of `progress_at` and `output_at`; the threshold is
  unchanged (`NO_SIGNAL_MINUTES`, `VITE_NO_SIGNAL_MINUTES`).

## 3.7 Delegate client

- `argus-delegate` runs claude with `--output-format stream-json --verbose` and follows
  its own stdout file. Events become short lines (assistant text, `▸ Tool arg`,
  truncated tool results, errors), redacted best effort with the 3.2 patterns (text
  before its `\r`/newline split as well as per line, as in 3.1), and are batched
  every ~3 s. After ~30 s of silence it adds `delegate alive, Ns since last
  output`, so No signal reflects a live process.
- `argus-delegate agent` (Cursor) runs `--output-format stream-json` (plain `json`
  prints nothing until the final result, so the log stayed silent) and follows its
  stdout the same way. Shown: the assistant's text, `▸ tool arg` for each tool call,
  failed tool results, retry/connection notices, and the final status. Never shown:
  the echoed prompt (`user`), `thinking`, successful tool output. A partial-delta
  stream is shown line by line and not repeated when the whole text follows. After the
  last turn the CLI waits for running background shells before it prints its result, so
  the delegate passes `--background-shell-timeout` (120 s, `ARGUS_AGENT_BG_SHELL_TIMEOUT_S`
  to change) to bound that wait. The final report, usage and exit status are read from
  the stream's `result` event.
- The batches are appended as JSON lines `{v, task_id, attempt, at, lines}` to
  `argus-task-log.jsonl` in the agent group directory (beside the model-policy
  snapshot, no new mount). The file is capped at 8 MiB per run; a relay failure is
  silent and never fails or slows the run. The token is never in that environment;
  environment values whose names contain `KEY`, `TOKEN`, `SECRET` or `PASSWORD` (8+
  characters) are masked as exact secrets.
- The directory is writable by the container, so the channel never follows a link:
  at task start it unlinks the file and creates it exclusively (`O_EXCL|O_NOFOLLOW`,
  parent checked to be a real directory), and reads it with `O_NOFOLLOW`, refusing
  anything but a regular file with one link (the relay stops). A record over 4 MiB
  without a newline is skipped.
- The channel (`integrations/nanoclaw/argus.ts`) recreates the file at task start, polls
  it every 2 s, keeps only records of its current task and attempt, and posts one batch
  per poll with `source: "delegate"` (<= 200 lines, 60 KiB). `409` `stale_attempt`,
  `not_assignee`, `not_claimed` or `401` stop the relay; other `409`, `413`, `422` drop
  the batch; `429`, `5xx` and network errors back off (2 s doubling to 30 s) and retry.
  Before COMPLETE/FAIL/ASK it drains the file within a 5 s deadline: it keeps reading
  while unread bytes remain, and paces each post 1.1 s from the last accepted one (or
  the back-off), so the drain does not cause a `429`. It waits at least 50 ms between
  polls and gives up (one warning) after three consecutive failed polls.

## 3.8 Worker (harness) client

- The harness (Claude Agent SDK in the agent container) is not an `argus-delegate`
  run, so its stream never reaches 3.7. Without a worker source the Live panel's
  **Harness** and **All** tabs stay hidden (they only appear when that source has
  lines). The harness therefore posts its own live log.
- Each SDK message of a claimed attempt is turned into short lines the same way as
  the Claude executor in 3.7 (assistant text, `▸ Tool arg`, truncated tool results,
  `claude finished` / error on `result`; never the echoed prompt or thinking). Lines
  are redacted best effort and appended as JSON lines `{v, task_id, attempt, at,
  lines}` to `argus-harness-log.jsonl` beside the model-policy snapshot (same
  directory and safety rules as 3.7: exclusive create, `O_NOFOLLOW`, 8 MiB cap, silent
  on failure). Outside a pinned task context nothing is written.
- The channel recreates that file at task start, polls it with the same cadence and
  batching as 3.7, and posts with `source: "worker"`. Before COMPLETE/FAIL/ASK it
  drains the harness file together with the delegate file. Codex and other harness
  engines are not parsed yet (open question).

# 4. Open Questions

- Only the `claude` executor is parsed into readable lines; other executors give
  keepalive lines only. The harness live log is Claude-only for the same reason.
- Retention: no path deletes tasks, so a task's log lives as long as the task, bounded
  to 2000 lines for each of three attempts. A future task purge must delete its
  `task_log` rows first (the foreign key refuses otherwise).
- A per-batch rate limit of one second may be too tight for a very chatty executor;
  the client batches, so it is not expected to matter.
- A row where a `\r` splits a secret mid-token and another rule (say a `Bearer`
  value) also matches is masked best effort: the row is kept whole or split by
  whichever leaves less unmasked.
- U+0085, U+2028 and U+2029 no longer split lines (only `\n` and a bare `\r` do), so
  such a line shows as one line.

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
