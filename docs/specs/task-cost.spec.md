---
status: implemented
created: 2026-10-29
updated: 2026-11-11
---

# Task cost and budget

# 1. Purpose and Scope

Argus knows what each task spends. Every usage row carries its `task_id` (engine rows
per harness turn, executor rows per delegated CLI attempt;
[account usage](account-usage.spec.md) 3.8, 3.9), and the [cost
view](cost-view.spec.md) prices tokens. But the only place to read it is the Cost tab,
grouped by account or project over 24h to 30d. Nobody watching a task sees what it has
cost so far, and nothing stops a task that loops through attempts from spending
without limit. `max_attempts` caps attempts, not money.

This spec adds two things:

1. **Task cost.** What a task has spent, per attempt and in total, with the cost
   view's honest figures, on the task view, live while it runs, and over REST and MCP.
2. **An optional budget.** `budget_usd` on a task. Crossing it steers the running
   attempt to wrap up, and no further attempt starts: the task is **blocked** as
   `budget_exhausted` until a manager raises the budget or cancels it.

# 2. Non-Goals

- **No billing.** Figures stay **estimates** at API rates (cost view 3.2), including
  for subscription accounts. A budget caps estimated spend, not an invoice.
- **No kill.** A crossed budget never cuts a lease or stops a process mid-turn.
  Argus has no interrupt (task steering 2). The attempt in flight may overrun by
  what it spends before it ends (3.5).
- **No project, account or fleet budgets.** Per task only. A default for new tasks
  is a setting (3.4), not a pooled budget.
- **No token budget.** Dollars only, so one figure is compared across models.
- **No provider billing calls**, and no change to the final usage report; the
  provisional record (3.8) sits beside it.

# 3. Decisions Made

## 3.1 Figures

- A task's cost is the cost view's group figures (cost view 3.1) over its usage rows:
  `runs`, `measured_runs`, `not_reported_runs`, the three token fields,
  `estimated_cost_usd`, `reported_cost_usd`, `unpriced_runs`, with the same price
  table and the same rules. A `not_reported` or unpriced run is never 0; a task with
  such runs is marked **partial**.
- **Per attempt.** `account_usage.attempt` ships with [account usage](account-usage.spec.md)
  3.10 (migration `0053_attempt_usage.sql`): the relay passes it in the body and the
  API resolves it against the agent's claims. Rows it could not attribute have
  `attempt = null` and count only in the total.
- One `GROUP BY attempt, model` over the existing index `account_usage_task`, priced
  in Python as the Cost tab is (cost view 5). No stored total: the rows are the truth.

## 3.2 Reading

- `GET /api/tasks/{id}/cost`: `{pricing, totals, by_attempt: [{attempt, …figures}],
  budget_usd, budget_state}`. `pricing` is the cost view's block. Read scope is the
  task's (managers all, workers only tasks they have leased).
- `budget_state`: `none` (no budget), `ok`, `warning` (≥ 80% of the budget),
  `exhausted` (≥ 100%). It is computed on `known_cost_usd` (3.8) and is `partial_ok` /
  `partial_warning` when the task is partial, because the true figure may be higher.
- MCP `get_task_cost(task_id)`, same permissions, added to [MCP
  operations](mcp-operations.spec.md).
- Task list views do not carry cost (it would be an aggregate per row). The task
  view and the agent drawer do.

## 3.3 Live: SSE `task.cost`

- After a usage insert that names a task, a **transient** event (task live log 3.4)
  `task.cost` with `{task_id, attempt, estimated_cost_usd, provisional_cost_usd,
  known_cost_usd, partial, budget_usd, budget_state}`, computed in the same write
  transaction by the query of 3.1. Usage rows still emit no stored event (account
  usage 3.2).
- Engine rows arrive per harness turn, so the figure moves during an attempt. A
  delegated executor run is counted by its provisional record while it runs (3.8)
  and by its final row when it ends.

## 3.4 Setting a budget

- `TaskCreate` gains `budget_usd: float | null`, `> 0` and `≤ 10000`. A [routine](routines.spec.md)
  template may set it.
- [Task edit](task-edit.spec.md): `budget_usd` is editable in **every** non-terminal
  state, including `claimed` and `blocked`, because raising it is how a manager
  continues a task (3.6). Clearing it (`null`) removes the cap. A change records
  `task.edited` with `{budget_usd: {from, to}}`. On a task blocked `budget_exhausted`,
  an edit that leaves the budget no longer exhausted unblocks it in the same
  transaction; a raise still below the estimate saves and stays blocked.
- [Task defaults](task-defaults.spec.md): `task_default_budget_usd` (global,
  per-project override; default unset) fills `budget_usd` for a new task that
  omits it. An explicit `null` in the request, or a stored project `null`, keeps
  "no budget".
- Migration `0076_task_budget.sql`: `tasks.budget_usd`
  (nullable real), `tasks.budget_steered_attempt` (nullable integer, 3.5).

## 3.5 Crossing the budget during an attempt

- In the usage-insert transaction, when the task is `claimed`, `budget_state`
  becomes `exhausted`, and `budget_steered_attempt` is not the current attempt:
  Argus creates a steer for that attempt (task steering 3.1) with actor
  `system:budget`, and sets `budget_steered_attempt`. Text:

  > The task's budget of $X is spent (estimate $Y). Stop starting new work: commit
  > and push what you have, then finish with ASK: describing what is left.

- Only once per attempt. When the assignee does not report the `steer` feature, no
  steer is created, and the attempt runs to its own end.
- The steer's limits (10 pending, one per second) apply. A refusal there is logged
  and changes nothing else.
- `system:budget` is a new actor kind. It is accepted only from this code path, never
  from a token.

## 3.6 No new attempt over budget

- Every path that would start another attempt checks the budget first: a requeue
  after a fail (failure classes 3.2), lease expiry, release, and claim. When
  `budget_state` is `exhausted`, the task goes to `blocked` with reason
  `budget_exhausted` instead, and spends no attempt.
- `budget_exhausted` is a block, not a failure: it is outside `FAILURE_CLASS`. The
  inbox shows it like any block, with the figures, the next action
  `raise_task_budget`, and the actions **Raise budget** (an edit, 3.4) and
  **Cancel task**; the task view offers the same pair. Unblock is refused
  (`409 budget_exhausted`) while the budget is still exhausted, and the dashboard
  hides it for this reason.
- A `COMPLETE:` is never refused for budget: finished work over budget still lands
  in review or succeeds. An `ASK:` blocks as usual.
- A partial task (unpriced or unreported runs) is judged on its known figure only.
  The block reason says "partial" so the manager knows it may be higher.

## 3.7 Dashboard

- Task view and agent drawer: a **Cost** tile with the estimate (labelled), the
  reported figure beside it when present, tokens, "partial" when it applies, and,
  with a budget, a bar against it (amber at warning, red at exhausted). A table
  per attempt with model and runs. Provisional spend (3.8) shows apart, labelled
  "provisional" while in flight and "interrupted" when its run ended with no
  final figure. Refreshed by `task.cost`.
- Task create and edit forms: a `Budget (USD)` field. The create form prefills it
  from the project's effective task defaults ([task defaults](task-defaults.spec.md)
  3.6); emptying it sends "no budget".
- The Cost tab is unchanged.

## 3.8 Provisional executor usage

A delegated Claude run spends for minutes before its final usage row exists. Its
`stream-json` carries usage per assistant message, so the run reports a running
figure, which its final row replaces.

- **Record.** `usage_provisional` (migration `0079_usage_provisional.sql`), one row
  per run, keyed `(task_id, run_key)` (`run_key` of account usage 3.8): `attempt`,
  `account_id`, `agent_id`, `revision`, `model`, `engine`, the three token fields,
  `interrupted` and `at`. The figures are **cumulative** for the run, never deltas.
- **Report.** `POST /api/accounts/{id}/usage/provisional` with `{task_id, attempt,
  run_key, revision, model, engine, input_tokens, output_tokens,
  cached_input_tokens}`, `204`. The caller holds a lease on the account (`403`
  otherwise) and the task's claim at that attempt (`409 not_claimed`,
  `not_assignee`, `stale_attempt`, `lease_expired`). `run_key` is `r1:`. Only the
  NanoClaw host posts it (account usage 3.9); the container holds no token.
- **Idempotent upsert.** A revision at or below the stored one is a no-op, so a
  duplicate, a replay and a late, out-of-order record add nothing. A run whose
  final `measured` row exists ignores any provisional record. The same `run_key`
  under another attempt or account is `409 run_mismatch`.
- **Reset.** A restarted executor (a retry, a model switch) is a new run with a new
  start time, so a new `run_key` starting at revision 1. A run that restarts its
  revision under the same key is ignored until it passes the stored revision; the
  figure never goes back.
- **Reconcile.** The run's final `measured` row (account usage 3.8, 3.10, also the
  late upgrade of a placeholder) deletes its provisional row in the same write
  transaction, so the run counts once, by its final figures. A final
  `not_reported` row keeps the provisional row and marks it interrupted.
- **In flight or interrupted.** A provisional row is **in flight** while its
  attempt is the task's claimed attempt and no final row has marked it; otherwise
  it is **interrupted**: the run ended with no measured final row (a crash, a
  killed container, an unparseable result). An interrupted row stays, as the known
  lower bound of what the run spent; the attempt also gets its `not_reported`
  outcome (account usage 3.10), and the task is **partial**. A late measured final
  still replaces it.
- **Several runs per attempt** (model fallbacks, re-delegations) are one row each,
  summed.
- **Lower bound.** `stream-json` assistant messages carry the full input and cache
  figures but only part of the output tokens, so a provisional figure is a known
  lower bound, priced at its model like a final row.
- **Figures.** `GET /api/tasks/{id}/cost` and MCP `get_task_cost` keep `totals`
  and `by_attempt` for final rows only and add `provisional: {runs,
  in_flight_runs, interrupted_runs, input_tokens, output_tokens,
  cached_input_tokens, estimated_cost_usd, unpriced_runs}` (null when there is
  none, also per attempt) and `known_cost_usd`, the final estimate plus the
  provisional one. `budget_state`, the steer (3.5) and the no-new-attempt check
  (3.6) use `known_cost_usd`; the steer stays once per attempt.
- **Other executors** (`codex`, `agent`, `opencode`, `copilot`, custom and local)
  report no provisional figure: their output carries none that is verified. Their
  runs count when they end, as before; nothing is estimated in between.

# 4. Open Questions

- **Hard ceiling.** Should a second threshold (say 150% of the budget) stop renewing
  the lease, ending the attempt? That spends what the attempt did on nothing, which
  is why v1 does not do it.
- **Reported cost.** For API-key accounts that report `cost_usd` on every run,
  whether the budget should use it instead of the estimate (cost view 4).

# 5. Constraints

- Single node, SQLite, no new service. The figure of 3.1 is one indexed aggregate
  per task, run on read and inside the usage insert. That is one extra query per
  usage row, which is per harness turn, not per token, and per provisional record,
  at most one per run every 15 s.
- Writes use `tx.now`; no network call in the insert transaction. The steer is a
  row, delivered by the next heartbeat as usual.
- Estimates stay labelled everywhere (cost view 3.1).

# 6. Implementation Consequences

- API: this spec and its index link; the budget migration; `tasks/cost.py` reusing
  `accounts/pricing.py` and the cost view's group math; the route, `task.cost`
  event, budget checks in the requeue, release, expiry and claim paths; the steer
  with actor `system:budget`; `budget_usd` in `TaskCreate`, task edit, routines and
  settings; MCP `get_task_cost`; regenerated `openapi.json` and `schema.d.ts`;
  tests for partial figures, the once-per-attempt steer, the block and its unblock.
- Worker: the delegate passes `attempt` in a direct usage POST (the relay already
  does, account usage 3.10), and writes provisional Claude usage to its relay file
  for the host to post (3.8, account usage 3.9).
- Provisional usage: migration `0079`, the route, the reconcile in the usage insert,
  `provisional` and `known_cost_usd` in the read models; tests for idempotency,
  reconcile, no final row, several runs and the steer.
- Dashboard: the Cost tile, the per-attempt table, the budget field, the inbox
  actions.
- [Cost view](cost-view.spec.md) 2 ("No … budget enforcement") is narrowed to the
  Cost tab; enforcement lives here, per task.
- Negentropy: two task columns, one usage column, one route, one event, one block
  reason. It adds no new loop or stored total and reuses the price table, the steer
  and the block path.
