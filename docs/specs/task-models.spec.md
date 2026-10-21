# Task models

# 1. Purpose and Scope

An operator who knows a task needs a stronger (or cheaper) model can only change
the global [model policy](model-selection.spec.md) today, which affects every
task. This spec adds **optional per-task pins**, set at creation and editable while
the task is queued:

- `harness_model`: the engine that runs the worker loop and its model;
- `executor_model`: the delegated coding CLI and its model;
- `harness_account`: the one account the engine lease may use, e.g. the local
  Strata account or one custom (OpenRouter) account (3.4).

Scope: the task fields (REST, MCP, dashboard), the lease narrowing they imply,
the harness model the NanoClaw channel configures at claim, and the model
`argus-delegate` runs.

# 2. Non-Goals

- No change to the global policy, its revision or its chains.
- No availability check of a model ID: like the policy, IDs are opaque and the
  operator must use one the provider serves.
- No model pin for `opencode` or for `custom`/`local` accounts: their models
  come from the account ([custom accounts](custom-accounts.spec.md), [local
  harness](local-harness.spec.md)); such a task is pinned to the account itself
  (3.4).
- No change of a running attempt: an edit never reaches a claimed task ([task
  edit](task-edit.spec.md) 3.4).

# 3. Decisions Made

## 3.1 Fields

| Field | Shape | Provider it needs |
| --- | --- | --- |
| `harness_model` | `{engine: claude \| codex, model}` or `null` | `claude` → `anthropic`, `codex` → `openai` |
| `executor_model` | `{executor: claude \| codex \| agent, model}` or `null` | `claude` → `anthropic`, `codex` → `openai`, `agent` → `cursor` |

`model` is required and follows the policy's model-ID rule
(`[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}`). `null` (the default) means "follow the
policy and account priority", as before. A harness pin whose provider is
excluded by the task's `account_providers` is `422 validation_failed`
(`harness_model: engine claude needs provider anthropic, excluded by
account_providers`), at creation and on edit (checked against the merged task).

Both are editable fields of [task edit](task-edit.spec.md): editable in
`awaiting_approval`, `pending` and `ready` only, `null` clears, the object is
replaced whole, the audit diff is `{from, to}` (no secret can be in it), and
the attempt snapshot keeps them.

## 3.2 Leases

- **Engine.** A harness pin narrows the engine role to its provider, on top of
  `account_providers`: the claim gate, `awaiting_quota` parking and waking, and
  the engine lease all use the narrowed set. A task whose provider has no free
  account waits as `awaiting_quota`, never runs on another engine.
- **Executor.** An executor pin narrows the executor lease to its provider. The
  executor stays outside the claim gate, as today ([account
  selection](account-selection.spec.md) 3): when no account of that provider is
  usable the channel logs it and `argus-delegate` for the pinned executor stops
  with `auth_missing`; it never substitutes another executor.

## 3.3 Models

A pin is a fixed choice, like `--model`: no fallback chain, the same model on
every attempt, failures and review rejections included.

- **Harness.** At claim the channel configures `harness_model.model` instead of
  the policy chain; the attempt artifact reports `policy_map: task`.
- **Executor.** The pinned snapshot carries `executor_model` and names its
  `executor` as `preferred_executor` (over the trivial/local preference).
  `argus-delegate` precedence becomes `--model`, the task pin (only for the
  pinned executor), the account model, the policy chain, the native default;
  `model_source` is `task` when the pin is used.

## 3.4 Harness account

`harness_account` is an account id, or `null` (the default: any account the
rest of the task allows). The API also accepts the account's name and stores its
id. It exists so an operator can force a run onto an account that would never
win on priority: the Strata local account (`strata-local`) or one custom account
(`openrouter-deepseek-v4.1-flash`) while the Claude accounts outrank them.

- **Validation** (`422 validation_failed`, `harness_account: ...`, at creation
  and on any edit of it, `account_providers`, `harness_model` or
  `executor_model`, checked against the merged task): the account exists; its
  provider is an engine provider (`anthropic`, `openai`, `custom`, `local`); it is
  allowed by `account_providers`; it is the provider a `harness_model` engine
  needs (so a local or custom account takes no `harness_model`: the account
  brings its model); and a `local` account allows only an `executor_model` of
  `agent` (the Strata harness only orchestrates and delegates to Cursor, [local
  harness](local-harness.spec.md)). A disabled account is accepted: the task
  waits until it is enabled.
- **Leases.** The pin narrows the engine role to that one account, on top of 3.2:
  the claim gate starts the task only when the caller holds an engine lease on
  that account or the account is usable (available or past its reset, below
  `max_leases`) and its provider is in the caller's `runtime_providers`; the
  engine lease and the failover candidate after an exhaustion consider only that
  account. When it cannot serve, the task waits as `awaiting_quota` (no attempt,
  no failure) and never runs on another account, of the same provider or not;
  `wake_waiting` wakes it only for a free slot of that account (enabling or
  refilling the account wakes it). The executor role is not narrowed by it.
- **Channel.** A worker that still holds an engine account the task does not
  allow (another account than the pinned one, a provider outside
  `account_providers`, or another engine than `harness_model`'s) leases afresh at
  claim instead of waiting for the periodic re-evaluation, stopping the old
  runtime first.
- The pin is in the attempt snapshot and the audit diff (`{from, to}` ids). A
  deleted account leaves a pinned task waiting until the pin is edited or
  cleared.

# 4. Open Questions

- Whether to pin only the engine or executor (policy model) without a model.
- Whether an executor pin should gate the claim like the engine.

# 5. Constraints

- One additive migration (`0037_task_model_pins.sql`): two nullable JSON columns;
  `0038_task_harness_account.sql` adds the nullable `harness_account` id.
- Rules live in `argus_core` and `tasks.service`; REST, MCP and the dashboard
  hold none of their own.
- No new service, no credential in a pin, the audit or the snapshot.

# 6. Implementation Consequences

1. This spec, its index link, pointers in task edit and model selection.
2. Core models and validation; the migration; create, edit, claim gate, wake,
   snapshot and lease narrowing in the API; MCP `create_task`/`edit_task`.
3. NanoClaw: harness override at claim, the pin in the policy snapshot,
   preferred executor, `argus-delegate` precedence.
4. Dashboard: engine/executor and model selects on the create and edit forms,
   shown on the detail.
5. Tests on every layer.
6. Harness account (3.4): the column, validation, claim gate, wake, lease and
   failover narrowing, the channel's re-lease on an unfit held account, MCP and
   REST arguments, and an account select on the dashboard create and edit forms.
