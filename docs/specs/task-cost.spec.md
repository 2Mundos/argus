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
- **No provider billing calls**, and no change to how usage is reported.

# 3. Decisions Made

## 3.1 Figures

- A task's cost is the cost view's group figures (cost view 3.1) over its usage rows:
  `runs`, `measured_runs`, `not_reported_runs`, the three token fields,
  `estimated_cost_usd`, `reported_cost_usd`, `unpriced_runs`, with the same price
  table and the same rules. A `not_reported` or unpriced run is never 0; a task with
  such runs is marked **partial**.
- **Per attempt.** `account_usage` gains a nullable `attempt`. The relay already knows
  it (it validates `task_id` and `attempt`, account usage 3.9) and passes it in the
  body; a direct delegate POST takes it from the pinned task context. Rows from
  before this change have `attempt = null` and count only in the total.
- One `GROUP BY attempt, model` over the existing index `account_usage_task`, priced
  in Python as the Cost tab is (cost view 5). No stored total: the rows are the truth.

## 3.2 Reading

- `GET /api/tasks/{id}/cost`: `{pricing, totals, by_attempt: [{attempt, …figures}],
  budget_usd, budget_state}`. `pricing` is the cost view's block. Read scope is the
  task's (managers all, workers only tasks they have leased).
- `budget_state`: `none` (no budget), `ok`, `warning` (≥ 80% of the budget),
  `exhausted` (≥ 100%). It is computed on `estimated_cost_usd` and is `partial_ok` /
  `partial_warning` when the task is partial, because the true figure may be higher.
- MCP `get_task_cost(task_id)`, same permissions, added to [MCP
  operations](mcp-operations.spec.md).
- Task list views do not carry cost (it would be an aggregate per row). The task
  view and the agent drawer do.

## 3.3 Live: SSE `task.cost`

- After a usage insert that names a task, a **transient** event (task live log 3.4)
  `task.cost` with `{task_id, attempt, estimated_cost_usd, partial, budget_usd,
  budget_state}`, computed in the same write transaction by the query of 3.1. Usage
  rows still emit no stored event (account usage 3.2).
- Engine rows arrive per harness turn, so the figure moves during an attempt. A
  delegated executor run is counted when it ends.

## 3.4 Setting a budget

- `TaskCreate` gains `budget_usd: float | null`, `> 0` and `≤ 10000`. A [routine](routines.spec.md)
  template may set it.
- [Task edit](task-edit.spec.md): `budget_usd` is editable in **every** non-terminal
  state, including `claimed` and `blocked`, because raising it is how a manager
  continues a task (3.6). Clearing it (`null`) removes the cap. A change records
  `task.edited` with `{budget_usd: {from, to}}`.
- [Settings](settings.spec.md): `task_default_budget_usd` (global, per-project
  override; default unset) fills `budget_usd` for a new task that omits it. An
  explicit `null` in the request keeps "no budget".
- Migration `0053_task_budget.sql`: `tasks.budget_usd` (nullable real),
  `tasks.budget_steered_attempt` (nullable integer, 3.5), `account_usage.attempt`
  (nullable integer).

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
  inbox shows it like any block, with the figures and the actions **Raise budget**
  (an edit, 3.4) and **Cancel**. Unblock is refused (`409 budget_exhausted`) while
  the budget is still exhausted.
- A `COMPLETE:` is never refused for budget: finished work over budget still lands
  in review or succeeds. An `ASK:` blocks as usual.
- A partial task (unpriced or unreported runs) is judged on its known figure only.
  The block reason says "partial" so the manager knows it may be higher.

## 3.7 Dashboard

- Task view and agent drawer: a **Cost** tile with the estimate (labelled), the
  reported figure beside it when present, tokens, "partial" when it applies, and,
  with a budget, a bar against it (amber at warning, red at exhausted). A table
  per attempt with model and runs. Refreshed by `task.cost`.
- Task create and edit forms: a `Budget (USD)` field, prefilled from the setting.
- The Cost tab is unchanged.

# 4. Open Questions

- **In-flight executor spend.** A long delegated run is counted only when it ends, so
  the steer of 3.5 may come late. `argus-delegate` parses `stream-json`, which
  carries usage per assistant message. A provisional running figure (relayed, then
  replaced by the final row) would close the gap, at the price of a second kind of
  usage record.
- **Hard ceiling.** Should a second threshold (say 150% of the budget) stop renewing
  the lease, ending the attempt? That spends what the attempt did on nothing, which
  is why v1 does not do it.
- **Reported cost.** For API-key accounts that report `cost_usd` on every run,
  whether the budget should use it instead of the estimate (cost view 4).

# 5. Constraints

- Single node, SQLite, no new service. The figure of 3.1 is one indexed aggregate
  per task, run on read and inside the usage insert. That is one extra query per
  usage row, which is per harness turn, not per token.
- Writes use `tx.now`; no network call in the insert transaction. The steer is a
  row, delivered by the next heartbeat as usual.
- Estimates stay labelled everywhere (cost view 3.1).

# 6. Implementation Consequences

- API: this spec and its index link; migration `0053`; `attempt` through the usage
  ingest (`accounts/usage.py`, `record_usage`); `tasks/cost.py` reusing
  `accounts/pricing.py` and the cost view's group math; the route, `task.cost`
  event, budget checks in the requeue, release, expiry and claim paths; the steer
  with actor `system:budget`; `budget_usd` in `TaskCreate`, task edit, routines and
  settings; MCP `get_task_cost`; regenerated `openapi.json` and `schema.d.ts`;
  tests for partial figures, the once-per-attempt steer, the block and its unblock.
- Worker: the relay and the delegate pass `attempt` in the usage body.
- Dashboard: the Cost tile, the per-attempt table, the budget field, the inbox
  actions.
- [Cost view](cost-view.spec.md) 2 ("No … budget enforcement") is narrowed to the
  Cost tab; enforcement lives here, per task.
- Negentropy: two task columns, one usage column, one route, one event, one block
  reason. It adds no new loop or stored total and reuses the price table, the steer
  and the block path.
