# Task models

# 1. Purpose and Scope

An operator who knows a task needs a stronger (or cheaper) model can only change
the global [model policy](model-selection.spec.md) today, which affects every
task. This spec adds **two optional per-task pins**, set at creation and
editable while the task is queued:

- `harness_model`: the engine that runs the worker loop and its model;
- `executor_model`: the delegated coding CLI and its model.

Scope: the task fields (REST, MCP, dashboard), the lease narrowing they imply,
the harness model the NanoClaw channel configures at claim, and the model
`argus-delegate` runs.

# 2. Non-Goals

- No change to the global policy, its revision or its chains.
- No availability check of a model ID: like the policy, IDs are opaque and the
  operator must use one the provider serves.
- No pin for `opencode` or for `custom`/`local` accounts: their models come from
  the account ([custom accounts](custom-accounts.spec.md), [local
  harness](local-harness.spec.md)).
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

# 4. Open Questions

- Whether to pin only the engine or executor (policy model) without a model.
- Whether an executor pin should gate the claim like the engine.

# 5. Constraints

- One additive migration (`0036_task_model_pins.sql`): two nullable JSON columns.
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
