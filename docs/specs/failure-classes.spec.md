# Failure classes

# 1. Purpose and Scope

Every failed attempt is retried by one rule today (`failures + 1 < max_attempts`),
whatever went wrong. A failure that will repeat identically (no repository, an
executor that cannot log in) burns the whole attempt budget, and a model that
said the task cannot be done is asked again twice. Production data (2026-10-16
to 2026-10-24, [worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md)
P6 and the preflight half of P2): about 20 failures reported "no checkout in
/workspace" and were retried into the same empty workspace, and an outer `catch`
in the channel reported every error after the account step as "repository
credential unavailable".

This spec:

1. gives every failure reason one class, in one table;
2. lets the class pick the retry action;
3. adds a deterministic channel preflight that blocks with a typed reason before
   any model work, and replaces the blanket `catch` with one reason per step.

# 2. Non-Goals

- No owned workspace or worktree (P2's workspace half) and no session resume
  (P4). The `session` class keeps today's retry until P4 lands.
- No change to admission (`quota`, `account_unavailable`;
  [account selection](account-selection.spec.md)) or to difficulty escalation
  ([task difficulty](task-difficulty.spec.md)).
- No classification of free-form error text on the server beyond what Fleet
  already does.
- No new service; one nullable column.

# 3. Decisions Made

## 3.1 One table of classes

`argus_core.tasks.FAILURE_CLASS` maps each reason to a class. Fleet, the fail
service and the dashboard read it; nothing else maps reasons.

| Class | Reasons |
| --- | --- |
| `admission` | `quota`, `account_unavailable` |
| `environment` | `env_*` block reasons (3.3), `worker_env` (3.4) |
| `transient` | `provider_failure`, `channel_error`, `runtime_switch_failed` |
| `session` | `session_lost`, `stalled`, `timeout`, `lease_expired`, `agent_ended` |
| `task` | `worker_fail`, `context_overflow`, `verification_failed` |

A reason not in the table is `task`. Fleet's `classify_failure` keeps its
reasons and adds `failure_class` to each failure row and a count per class.

## 3.2 The class picks the action

| Class | Action | Spends an attempt |
| --- | --- | --- |
| `admission` | unchanged: requeue or `awaiting_quota` | no |
| `environment` | `blocked` with the `env_*` reason; inbox row; a manager unblocks | no |
| `transient` | requeue after a backoff (below) | yes |
| `session` | requeue at once, as today | yes |
| `task` | escalating reasons: requeue one difficulty up, as today; `worker_fail`: `blocked` as `worker_gave_up` | yes |

- **Backoff.** A `transient` fail that requeues sets `tasks.retry_at` to
  `tx.now + min(60 · 2^(failures − 1), 900)` s, `failures` counted after this
  fail (60 s, 120 s, 240 s, …, at most 15 min). `claim()` skips a ready task
  whose `retry_at` is after `tx.now`; any other transition clears `retry_at`.
  Migration `0047_task_retry_at.sql`.
- **Gave up.** A `worker_fail` that leaves attempts in the budget blocks the task
  as `worker_gave_up` instead of requeueing it, with the worker's text as `error`.
  The inbox shows it like any block; unblock retries it, cancel ends it. A
  `worker_fail` on the last attempt fails the task, as today.
- `max_attempts` keeps its meaning: the cap on attempts that count.

## 3.3 Channel preflight

After the account and repository lease, and before `/clear` and injection, the
channel runs these checks in order. The first one that fails calls
`POST /tasks/{id}/block` with its reason and a short `error`, then finishes the
task. No model session starts and no attempt is spent.

| Check | Reason |
| --- | --- |
| The task needs `repository:write` and its project has a repository URL | `env_repository_unconfigured` |
| The repository lease is taken and applied (today's step) | `env_repository_unavailable` |
| `git ls-remote <url> HEAD` through the applied credential answers within 20 s | `env_repository_unreachable` |
| The leased executor's credential works: `argus-delegate --probe` (below) exits 0 within 30 s | `env_executor_auth` |

- `argus-delegate --probe --engine <engine>` runs the engine's own credential check
  without a model call and prints one line. An engine with no such check passes.
  It reads the same environment as a real run.
- `BlockRequest` gains an optional `error` (at most 1,500 characters, redacted by
  the channel like the live log), stored as the task's `error`. A worker may block
  only with an `env_*` reason from `ENV_BLOCK_REASONS` or the reasons it sends
  today; anything else is `422`.
- Steps after the preflight fail with their own reason and the real error:
  page inlining and injection as `channel_error` naming the step. The blanket
  "repository credential unavailable" message goes away.
- A blocked `env_*` task shows in the inbox under its project, as blocked tasks
  already do ([MCP operations](mcp-operations.spec.md) 3.2).

## 3.4 The worker can say "environment"

The reply protocol gains `FAIL (environment): <why>`. The channel blocks the task
as `worker_env` with `<why>` as `error`, instead of failing it. The task prompt
says to use it only when the workspace, tools or credentials are missing, not
when the work is hard. Any other `FAIL:` is `worker_fail`.

# 4. Open Questions

- Should one `env_repository_*` block hold the project's other ready tasks until
  it is unblocked, or is one cheap preflight per task fine?
- Is blocking on the first `worker_fail` too strict for tasks that would pass on a
  plain retry? Re-check after two weeks against the baseline.

# 5. Constraints

- Single-node, SQLite only; all writes through `Database.write()`, times from
  `tx.now`. No network calls in a write transaction: the channel probes before it
  calls the API.
- Preflight probes are best-effort and bounded by their timeouts. A probe that
  cannot run (the tool is missing) is an `environment` block, not a pass.
- No credentials in `error` or log excerpts.
- English only.

# 6. Implementation Consequences

- Core: `FAILURE_CLASS`, `ENV_BLOCK_REASONS`; `transition` unchanged (the service
  picks `BLOCK` or `FAIL`).
- API: `fail` applies 3.2; `claim` honours `retry_at`; `BlockRequest.error`;
  migration `0047`; Fleet `failure_class`; `make openapi`.
- Channel: preflight steps and reasons, `FAIL (environment):`, per-step errors in
  place of the outer `catch`; `argus-delegate --probe`.
- Dashboard: inbox copy for `env_*`, `worker_env` and `worker_gave_up`; Fleet
  counts by class.
- Amend [worker lifecycle](worker-lifecycle.spec.md) (the fail and block
  paragraphs). Mark P6 and the preflight half of P2 as specified in the
  [worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md) draft.
- Tests per layer: one per class action, the backoff schedule, each preflight
  reason, and the environment FAIL directive.
