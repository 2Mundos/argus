---
status: implemented
created: 2026-10-26
updated: 2026-11-27
---

# Settings

# 1. Purpose and Scope

One place for operator choices that today live in scattered env vars, files and
views: **global settings** with **per-project overrides**. A setting is a typed
key from a registry in code; its effective value for a project is the project
override, else the global value, else the registry default.

Phase 1 ships the store, the API, the Settings tab and two settings:

- **Model preferences**: the [model policy](model-selection.spec.md) moves from the
  Engines view to Settings and gains project overrides.
- **Independent code review**: a project can require that code a model wrote is
  reviewed by a different model before the task succeeds (3.5).

Readable by any valid token. Written by admins.

Explicit complete per-difficulty routing is owned by
[execution chains](execution-chains.spec.md). Those opt-in lists own both layers
and models; the legacy selection rules below apply only without an effective
explicit list, or when task model/account pins choose the whole legacy pin path.

# 2. Non-Goals

- No free-form key/value store. Unknown keys are rejected; every key has a type,
  a default and a list of scopes it may be set at.
- No per-agent, per-pool or per-task settings. Tasks keep their own fields (pins,
  `review`, difficulty); a setting never rewrites a stored task.
- No change to a claimed attempt. Settings are read at the moment they apply
  (claim, completion); an attempt in flight keeps what it started with.
- No secrets in settings. Credentials stay in the SOPS file
  ([single node](single-node.spec.md) 3.6).
- Not a replacement for the PR's own CI or agentic review
  ([gitea helper](gitea-helper.spec.md) 3.4); that stays a separate signal.

# 3. Decisions Made

## 3.1 Registry

Each key is declared once in code with: `key` (dotted, e.g. `review.independent`),
a JSON Schema for its value, `default`, `scopes` (`global`, `project`, or both),
and a one-line description shown in the dashboard. The registry is served by
`GET /api/settings/registry`. Adding a key is a code change and a spec change.

## 3.2 Storage and resolution

- Table **`settings`**: `scope` (`global` or a project slug, FK to `projects`),
  `key`, `value` (json), `updated_at`, `updated_by`. Primary key `(scope, key)`.
  Absent row means "inherit".
- Effective value for project `p`: row `(p, key)`, else `(global, key)`, else the
  registry default. Each read reports `source`: `project` | `global` | `default`
  (and `env` where 3.4 applies).
- A value is validated against the key's schema on write (`422` naming the key and
  the problem). A key not settable at the requested scope is `422 scope_not_allowed`.
- Every write emits `settings.changed {scope, key, from, to}` in the same
  transaction; the Events view and audit trail are that table.

## 3.3 API, MCP, dashboard

- `GET /api/settings` — global values with `source`.
- `GET /api/projects/{slug}/settings` — effective values for the project, with
  `source` per key.
- `PUT /api/settings/{key}` and `PUT /api/projects/{slug}/settings/{key}` — body
  `{value}`; admin only.
- `DELETE` on the same paths — remove the row (revert to inherit); admin only.
- MCP: `get_settings {project?}` (any token), `set_setting {key, value, project?}`
  and `reset_setting {key, project?}` (admin), over the same routes.
- **Settings tab**: a scope picker (`Global` or a project) in the page toolbar.
  Keys are grouped into in-page tabs — **Orchestrator** (global only), **Tasks**
  (defaults, models, git identity), **Execution** (execution chains), and
  **Review** (delivery and review toggles, independent review, review policy) —
  instead of one vertical stack. Each key shows its effective value, where it
  comes from, and a "Reset to inherited" action on an override. Unsaved editor
  drafts (notably execution chains) are kept across live reloads and in-page
  tab switches until the operator saves, discards, or changes scope. The Engines
  view keeps engine readiness and links to Settings for model chains.

## 3.4 Model preferences

- Key `models`, scopes `global` and `project`. The global value is today's policy;
  storage, routes and precedence of [model selection](model-selection.spec.md) 4a
  stay as they are (`ARGUS_MODEL_POLICY` env, then `data/model-policy.json`, then the
  defaults), and `GET /api/settings` reports it with `source` `env` | `global` |
  `default`. `/api/models` remains and is what the Settings tab calls for this key.
- A **project override** is a partial policy: per map (`harness`,
  `implementation`, `trivial`, `hard_harness`, `hard_implementation`) and per
  engine, a chain replaces the global chain for that engine; anything not named is
  inherited. It is stored in `settings` and validated with the same rules.
- The effective policy for a task is resolved at claim for the task's project and
  sent in the claim payload (`model_policy`, `model_policy_revision`). The channel
  uses it instead of the host file and copies it to the workspace, so
  `argus-delegate` and the attempt evidence see the project's revision. The revision
  is computed over the effective (merged) policy.
- `ARGUS_MODEL_POLICY` still wins over everything, overrides included, and the
  response says so: it is the operator lock.
- Task pins ([task models](task-models.spec.md)) still beat any chain.

## 3.5a Review policy

Key `review.policy`, scopes `global` and `project` (project wins), admin only:

```json
{ "mode": "off" | "observe" | "enforce", "default": "standard" | "human",
  "rules": [{ "name": "docs", "tier": "skip" | "standard" | "human",
              "paths": ["docs/**"] }] }
```

Default `{"mode": "off", "default": "standard", "rules": []}`. At most 50 rules, rule
names unique (1 to 40 of `[a-z0-9-]`), 1 to 50 repo-relative POSIX globs each; a
malformed value is a 422 naming the field. `default` is never `skip`. Semantics:
[review policy](review-policy.spec.md). The forge agentic-review policy
(`review.forge_agentic`) is unchanged.

## 3.5 Independent code review

Key `review.independent`, scopes `global` and `project`, value:

```json
{ "mode": "off" | "different_model" | "different_provider", "max_rounds": 3,
  "max_reviewer_failures": 2, "max_repairs": 0,
  "verify_findings": "off" | "observe" | "enforce" }
```

Default `{"mode": "off", "max_rounds": 3, "max_reviewer_failures": 2, "max_repairs": 0,
"verify_findings": "off"}`; `verify_findings` is read from the value when set and is
`off` otherwise ([review finding verification](review-finding-verification.spec.md) 3.4). Dashboard label: **Require review by a
different model**, with `different_provider` shown as "…from a different provider".

- **Applies to** tasks that deliver code: completion carries a PR or commit
  reference ([task delivery](task-delivery.spec.md) 3.1). Other tasks (research,
  answers) skip it, with a `review.independent_skipped {reason: no_delivery}` event.
- **Authors** of a task are every model recorded in its
  [model observability](model-observability.spec.md) executions, harness and
  delegate, across all attempts: `observed_model` where present and
  `configured_model` too, because an unknown observation must not make two runs
  look different. The reviewer must also differ in model vendor, derived from the
  model (reviewer route below), in both modes.
- **Gate.** On completion with the mode on, the task moves to `in_review` and Argus
  creates a child task of kind `code_review` in the same project, `prompt_trust:
  orchestrator`, read-only, with input `{task_id, pr, head_sha}`. Its model comes
  from the reviewer route below (the author's `execution.chains`); with no
  chain configured, its
  harness and executor are pinned ([task models](task-models.spec.md)) to the
  first model in the project's effective policy chains that is not an author (or,
  for `different_provider`, whose engine differs), among engines with a usable
  account.
- **Result envelope.** The child's spec gives the exact COMPLETE shape: `data` with
  exactly `verdict`, `findings` and `head_sha`, sent with the `send_message` tool to
  `argus`. A `code_review` completion whose `data` is absent but that carries those
  keys at the top level (the channel forwards them as `envelope`) has them moved into
  `data` and records `review.result_normalised`; no other kind takes an envelope, and a
  missing key is never invented. A `data` that fails the schema is a 422 whose details
  name the offending or missing keys and the expected keys.
- **Reviewer route.** There is no review-specific chain: the reviewer is picked from
  `execution.chains` ([execution chains](execution-chains.spec.md) 3.1). Argus walks
  the chain of the reviewed task's difficulty in order, then the other difficulties
  (sorted difficulty names, then chain order), and skips every alternative that may be
  an author (for a paired alternative only its executor is compared, since the harness
  only coordinates; both selectors still need a usable account; a single alternative is
  compared by its harness): one whose model is an author's, or whose model vendor is an author's
  (derived from the model: OpenRouter `vendor/model` prefix or the `claude-*`, `gpt-*`,
  `gemini-*` family; `custom` and `local` are routers, never vendors, and an unknown
  vendor is not excluded on provider alone), or, when an account-source model is
  unknown until lease (local), whose account served the reviewed task. Alternatives that already ended a
  review without a verdict, or have no usable account or no worker that can run them,
  are skipped too (falling back to the legacy model policy only when
  `execution.chains` configures no difficulty). The rest, in order, become the child's explicit route
  (execution chains 3.7): the first runs it and the others are its fallbacks. Argus
  records `review.independent_selected {child_id, selected, fallbacks, skipped,
  chain_source, difficulty}` (`chain_source` is `execution.chains`) on the reviewed task, and the child's route view shows
  the same. If no eligible alternative remains, the gate blocks with
  `no_independent_reviewer`, the skipped list, and one actionable `review` decision
  (`retry_reviewer`, `approve_without_review`, `cancel`); an author is never used
  and model IDs are never invented.
- **Verdict.** The review task's result data is `{verdict: approve |
  request_changes, findings: [...]}` with `head_sha`. `approve` for the current
  head passes the gate; the task then goes through its own `review` gate if it has
  one (human/orchestrator decision), else `succeeded`. `request_changes` returns
  the task to `ready` with the findings appended to the spec under **Review
  findings**, without counting a failure, like a review rejection today.
- **Proof, not trust.** The gate passes only if the review task's own executions
  show no author model (or provider). Otherwise the verdict is void, recorded as
  `review.independent_void {reason: same_model}`, and a new reviewer is chosen.
- **No silent skip.** If no eligible reviewer model has a usable account, the task
  stays `in_review` with `block_reason: no_independent_reviewer` and one actionable
  `review` decision (`payload: {gate: review_failed, reason: no_independent_reviewer}`)
  offering `retry_reviewer`, `approve_without_review` and `cancel`.
- **Reviewed head.** The head is the commit the reviewer reads (`code_review` input
  requires `head_sha`). When the forge reports the live head for the task's PR
  (read before the completion write and matched case-insensitively to the same PR),
  that live full head SHA takes precedence over older attempt artifacts; otherwise
  it comes from the current attempt's result/progress, the newest PR artifact's own
  `head_sha`, or the newest commit milestone in progress history. An older PR's
  `head_sha` is never carried onto a replacement PR. A PR's bound artifact is read
  back after the completion's own write, so the gate sees it. **A merged PR is
  reviewed too**, at the head that merged: a merge never skips the gate, so the
  parent always gets a reviewer, a decision or a verdict. If no head can be read,
  the task stays `in_review` with `block_reason: no_review_head`,
  `review.independent_blocked {reason: no_review_head}`, and one open `review`
  decision (payload `{gate: review_failed, reason: no_review_head, failed: []}`)
  with the choices `retry_reviewer`, `approve_without_review` and `cancel`; a
  second block adds no decision. `retry_reviewer` reads the forge head again,
  before its write. No child is created while the head is unknown. The block is
  reached only when neither the record, the forge nor the milestone history names
  a head.
- **Approval of a forge-gated parent.** An `approve` verdict on the exact PR and
  exact head (`review.independent_passed {child_id, verdict, pr, head_sha}`) is not
  proof of forge state and never approves a different SHA or replacement PR
  ([pre-merge independent review](pre-merge-independent-review.spec.md)). For a
  `pr_green` or `pr_merged` parent with `review=none` and acceptance off, with no
  open `review` decision, the approval alone is the review it needs: the completion
  route reads the parent's PR from the forge before the write
  (`completion_forge_read`), and the write accepts the parent (`succeeded`, no
  `review` decision) only when `check_accept` holds for that read (merged, or open
  with green checks for `pr_green`), the read is at most 120 s old, its checks
  belong to its head, and both the PR identity and head equal the reviewed PR and
  head. Otherwise the parent stays `in_review` with `block_reason: awaiting_forge`
  and one `review.independent_awaiting_forge {why, head_sha}` event per distinct
  reason, and no decision. A head or PR that changed after the review voids the
  approval (`review.independent_void {reason: head_moved | pr_moved}`) and spawns a
  reviewer for the new PR and head. A loop (`review_settle`, every
  `merge_supersede_interval_s`, backing off per task) reads the forge outside any
  write for each `awaiting_forge` parent and the write re-checks it before
  accepting; if manual review or acceptance was switched on meanwhile it opens the
  regular `review` decision instead. With `review` other than `none`, acceptance
  on, or open acceptance evidence, the approval opens the `review` decision as
  before, and accepting it still verifies that the forge's PR and head match the
  independent review approval (unless waived). `request_changes`, reviewer failure
  and the callbacks are unchanged and idempotent (a parent no longer `in_review` or
  already processed for that child ignores the callback). Startup repair skips
  `awaiting_forge` parents.
- **Reviewer failure.** A `code_review` child that ends `failed` or `cancelled`
  without a verdict does not strand the parent. Argus records
  `review.independent_child_failed {child_id, state, error_class, account, model,
  alternative}` on the parent and selects again with that alternative (and any
  alternative pinned to its account) skipped as `failed`, exactly like a void
  verdict; the next alternative gets a new child. Auth errors are not special-cased.
  `max_reviewer_failures` (0-20, default 2) bounds the failed children per
  window, independent of `max_rounds`: up to that many failures reselect, the next
  one escalates. When no alternative remains, or the bound is hit, the parent stays
  `in_review` with `block_reason: review_failed` and one open `review` decision
  (payload `{gate: review_failed, failed}`) for a human, with the choices
  `retry_reviewer` (starts a new window and spawns a child, skipping nothing),
  `approve_without_review` (explicit and audited: `review.independent_waived
  {decision_id, notes, failed}`, then the parent goes through its own `review`
  gate or `succeeded`) and `cancel`. An author model is never selected as a
  fallback. The callback is idempotent per child. The delivery summary
  (`independent.failed_reviewers`) lists each failed attempt with its child link.
- **Repairs.** `max_repairs` (0-3, default 0) bounds the worker attempts that repair
  an approved PR whose checks fail or that stopped being mergeable, in total per
  task and at most one per approval. At 0, such a PR opens a decision for a person.
  A closed PR always does. See [PR steward](pr-steward.spec.md). Dashboard label:
  **Repair an approved PR that breaks (attempts)**.
- **Manual review when the gate cannot deliver.** A task in `in_review` always has
  an active `code_review` child or an open `review` decision, never neither. When
  the last reviewer is gone without a verdict and the retry decision above does not
  apply, Argus opens the regular `review` decision (`accept`, `accept_with_deviation`,
  `reject`; the same acceptance rules) for a human, with `gate` in its payload and
  the reason in its question: `no_reviewer` (no eligible reviewer; no head is
  covered by the `no_review_head` decision above), `reviewer_failed` (a reviewer finished
  without an `approve` or `request_changes` verdict) or `gate_disabled` (see
  Timing). `failed` lists the failed children. One open decision at most, and a
  repeated callback adds none. Answering it clears the gate `block_reason`.
- **Timing.** The mode is read when the task completes. Turning it on does not
  reopen succeeded tasks. Changing `review.independent` (set or reset, any scope)
  cancels the running reviewer of every `in_review` parent in a project where it is
  now `off`; that parent gets the `gate_disabled` decision.
- **Startup repair.** On start, every `in_review` task with neither an open
  `review` decision nor an active reviewer child gets one (`reviewer_failed` when
  a reviewer child ever existed, else `no_reviewer`) and a
  `review.decision_backfilled {decision_id, gate}` event. State is unchanged and a
  second run finds nothing.
- **Not the forge's review.** This gate is Argus's own `code_review` child. It
  never asks for, waits on, or counts a comment on the pull request. Whether a
  worker needs the forge's agentic review is a separate key (3.12).

## 3.6 Routines

Key `routines.enabled`, scope `global` only, boolean, default `true`. Off pauses
every [routine](routines.spec.md) without editing them: the scheduler creates no
runs and leaves `next_run_at` as it is, so turning it back on gives each due
routine one catch-up run. Run now still works while it is off.

## 3.7 Difficulty executors

Key `difficulty.executors`, scopes `global` and `project`. Value: a partial map
of difficulty (`trivial` | `simple` | `standard` | `hard`) → ordered list of
executor CLIs (`claude` | `codex` | `opencode` | `agent` | `agy`), or `[]` / `["none"]`
for no delegated executor ([task difficulty](task-difficulty.spec.md)). Default
`{}` means the built-in defaults for every level. Unnamed levels inherit those
defaults. `ARGUS_DIFFICULTY_EXECUTORS` (same JSON shape, validated at startup)
wins over the store when set, reported as `source: env` like `models`. The
Settings tab no longer edits the map (the API and MCP still do); it only shows the
effective order beside **Execution chains**. Claim sends the effective chain for the task's
difficulty as `difficulty_executors`.

This is a legacy executor-only setting, not a list of complete execution routes.
An empty list removes delegation but leaves the harness account ranking unchanged,
including trivial tasks' soft local preference and possible cloud fallback. The
[Execution chains contract](execution-chains.spec.md) defines the opt-in general
replacement: complete paired or single-layer alternatives for every difficulty,
per-level inheritance, compatibility and explicit migration. Setup Settings and
the shared settings API/MCP expose it; no automatic conversion is defined.

## 3.8 Git identity

Key `git.identity`, scopes `global` and `project`. Value: `{name, email}`, both
required and non-empty (`email` must look like an address, no spaces). Default
`{}` means not configured. It is the author and committer of worker commits. A
project value takes precedence over the global one
(`effective_git_identity`); with neither set it fails closed, naming
`git.identity`. There is no built-in default and the agent name is never used.
Delivery has two layers. The global value is passed to each pool container at
start as `ARGUS_GIT_AUTHOR_NAME`/`ARGUS_GIT_AUTHOR_EMAIL`; this pinned value is
the fallback. At claim, the response carries `git_identity` (`{name, email}`):
the effective project-over-global value for that task's project only, or `null`
when neither scope is set. The NanoClaw worker puts it in its own process
environment as those same two variables for the duration of that task, so the
session container it spawns inherits it and it wins over the pool's pinned
value, then restores the pool's value when the task ends. Nothing is written to
git config, a file or the image, and another project's task never sees it. A
task without an identity (`null`) leaves the pool value in force; if that is
unset too, the worker's git refuses to commit or push
([worker lifecycle](worker-lifecycle.spec.md)). Changing a project override
does not touch the global value or any other project, and resetting it
(`DELETE`) restores inheritance. A change reaches tasks claimed after it; a
running task keeps the identity it was claimed with. Other engines and runtimes
that do not run the NanoClaw worker get no task-scoped identity and use only
their own pinned value.

The Settings tab has a Git author card (global value, or the project override
under a project scope) and the Projects page shows per project whether it
inherits the global value or overrides it, with an explicit "Reset to inherited"
action. Both validate a non-empty name and an address-shaped email before
saving, and show permission and save errors. The inherit/override state is the
`source` of the existing read model (`project` is an override; `global` is
inherited; `default` is not set).

## 3.9 Command guard

Key `command_guard.enabled`, scopes `global` and `project`, boolean, default
`true`. On keeps the Claude Code `PreToolUse` [command guard](command-guard.spec.md)
active; off disables it for new attempts. A project value takes precedence over
the global one. The claim carries the effective value as `command_guard_enabled`;
the NanoClaw channel sets `ARGUS_COMMAND_GUARD=off` for that attempt when it is
false, so a change applies to attempts claimed afterwards.

## 3.10 Auto-merge

Key `delivery.auto_merge`, scopes `global` and `project`, boolean, default
`false`. A project value takes precedence over the global one. It applies only to
tasks whose [deliverable](task-deliverable.spec.md) is `pr_merged`, whoever created
them: the operator's setting is the grant, so a task's own `input` is not consulted.
On, for such a task:

- the claim carries `auto_merge: true`, and the worker's prompt authorizes it to
  merge its own pull request once the delivery preconditions hold (the same
  preconditions as `delivery.allow_merge`: exact head, required checks green,
  review findings resolved);
- a completion succeeds only with the PR merged on the forge
  ([task deliverable](task-deliverable.spec.md) 3.4): an open or unreadable PR is
  refused and the worker keeps its claim to merge and complete again. The task does
  not wait in `in_review` for a manager to confirm the merge. Its own `review`,
  checklist and independent-review gates still apply.

Off, `pr_merged` behaves as before: the worker may not merge unless the task input
grants it, and the merged PR is confirmed by a decision. The claim carries the
effective value (`auto_merge`), so the change applies to attempts claimed
afterwards; the completion gate reads the setting when the worker completes.
Other deliverables are unaffected. Admin only, like every setting; a worker cannot
widen it.

## 3.11 Acceptance

Key `review.acceptance`, scopes `global` and `project`, boolean, default `true`
(today's behaviour). A project value takes precedence over the global one. On, a
completed task waits in `in_review` for acceptance whenever its `review` mode, an
open checklist item, a task with no structured acceptance that already holds a PR,
or a green or merged deliverable still to confirm says so.

Off, a completion never enters `in_review` for those reasons: the task goes
straight to `succeeded` and no review decision opens. Checklist reports are still
stored. A `pr_green` or `pr_merged` deliverable does not lose its evidence: the
completion needs the forge to show the PR merged (or, for `pr_green`, open with
green checks on the head) at that moment, read before the write
([task deliverable](task-deliverable.spec.md) 3.4); anything else is `422
deliverable_missing` and the worker keeps its claim. Unconfirmed is never success.
It is read when the worker completes. The success is recorded as `not_reviewed`
(`acceptance_off`), and acceptance items without evidence stay
`verification_missing` ([task deliverable](task-deliverable.spec.md) 3.10).

It does not change independent review (3.5), which is its own opt-in and still holds
a code task in `in_review` while on, nor the manager operations that move a task
there on request ([task deliverable](task-deliverable.spec.md) 3.8).

## 3.12 Forge agentic review

Key `review.forge_agentic`, scopes `global` and `project`, boolean, default `false`.
A project value takes precedence over the global one. It says that the project's
forge posts an agentic review on pull requests ([gitea helper](gitea-helper.spec.md)
3.4) and that a worker must have one for the exact head before it calls a PR ready or
merges it. It is independent of `review.independent` (3.5): that key off does not
turn this one on, and this one off does not disable that gate. Nothing is inferred
from the forge's name, from missing comments, from a workspace file or from task
text; a project without a configured agentic review is simply `false`.

The claim carries the effective value as `forge_agentic_review_required`. The
NanoClaw channel turns it into the attempt's trusted policy, `ARGUS_FORGE_REVIEW=
required|off` in the worker host environment (forwarded to session shells like the
git identity), and words the repository part of the task prompt from it:

- **Required.** The worker waits for the agentic review of the exact head
  (`argus-task wait review`) and resolves its P1–P3 findings. `pr-ready` reports
  `review_missing` while none covers the head, and `--skip-review` cannot lift it.
- **Off.** A missing agentic review is not a blocker and the worker is told not to
  wait for or request one. It is not permission to ignore what exists: `pr-ready` still
  reads the review for the head and reports `review_findings` for any finding count above
  zero, and still reports `changes_requested`, failed or unknown CI, `not_mergeable` and
  `head_moved`. `--skip-review` does not remove these either.
- **Unknown.** An older API that omits the field, or a value other than `off`,
  counts as required. Only a standalone `argus-task` with no policy at all
  (`ARGUS_FORGE_REVIEW` unset, outside an Argus session) keeps the legacy
  `--skip-review` ([task helpers](task-helpers.spec.md) 3.6).

The task read shows the same decision as `forge_review` (`required` | `off`) and, for a
bound PR, what the readiness gate would report in `pr_gate`
([worker lifecycle](worker-lifecycle.spec.md) 3.7). These are decisions, resolved by the
function the claim uses, not hints.

`delivery.auto_merge` (3.10) stays an authorization: it allows a merge once every
precondition holds and does not decide which review is one of them. The policy is read
at claim, so a change applies to attempts claimed afterwards. Upgrading changes one
default: a repository whose workers relied on the always-required agentic review needs
`review.forge_agentic` set to `true` for it (globally or for that project).

## 3.13 Task defaults

Keys `task_default_deliverable`, `task_default_priority`, `task_default_difficulty`,
`task_default_required_capabilities`, `task_default_budget_usd`,
`task_default_max_attempts` and `task_default_timeout_s`, scopes `global` and
`project`: what a new task gets for a field it leaves out. Each key resolves on
its own, so a project that overrides one inherits the rest. Values, precedence,
the creation snapshot and `GET /api/projects/{slug}/task-defaults` are in
[task defaults](task-defaults.spec.md).

# 4. Open Questions

- Should orchestrators be allowed to write project overrides? Phase 1 says admin
  only, because `review.independent` is a guardrail an orchestrator should not be
  able to weaken. A split (orchestrators may tighten, not loosen) is possible later.
- Comparing models by ID treats `claude-sonnet-5-5` and `claude-opus-5-5` as
  different. Is that independent enough, or should `different_model` mean a
  different family? `different_provider` exists for the strict case.
- Should the PR's agentic review ([gitea helper](gitea-helper.spec.md) 3.4) count
  as an independent review when its model is known? Needs a model on that comment.
  Until then the two are separate keys (3.5, 3.12).
- `ARGUS_FORGE_REVIEW` is a process environment variable: a worker with a shell could
  set it for one command. It is a guard against mistakes and stale prompts, not against
  a worker that bypasses its own helper; the merge itself is still checked on the forge.
- Candidates for later keys: default task `review`, lease TTL,
  `ARGUS_MAX_TASKS_PER_AGENT_HOUR`, workspace cleanup.

# 5. Constraints

- SQLite only; one table, written through `Database.write()`, time from `tx.now`.
- Resolution is a pure function of the rows and the registry, so the claim path
  and the dashboard compute the same value.
- The model-policy revision stays identical between the API and
  `model-policy.mjs`; the merged policy is hashed by the same canonical rule.

# 6. Implementation Consequences

- Migration `NNNN_settings.sql`: `settings` table.
- `settings` module: registry, validation, resolution; REST routes and MCP tools.
- Claim payload gains `model_policy` and `model_policy_revision`; the NanoClaw
  channel prefers them over `ARGUS_MODEL_POLICY_PATH`, which stays as a fallback for
  older APIs.
- Kind `code_review` registered with its result schema; completion path checks the
  setting and creates the review child; reviewer selection reads the effective
  policy and account availability.
- Dashboard: Settings tab; the model chain editor moves there from Engines.
- Update [model selection](model-selection.spec.md) 4a and
  [worker lifecycle](worker-lifecycle.spec.md) 3.2 (the `in_review` path) in the
  same PR as the code.

## Verification boundary

- Commands:
  - `uv run pytest -q -p no:cacheprovider apps/api/tests/test_settings.py apps/api/tests/test_forge_review_policy.py`
  - `pnpm --filter @argus/dashboard test` runs the dashboard suite, including the views that read settings.
- Verified: unit and fake-API level against an in-memory database; the dashboard under vitest with a mocked API.
- Not verified: the settings page in a real browser, setting changes taking effect across a restarted node, and any production node.
