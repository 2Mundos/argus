---
status: draft
created: 2026-11-21
updated: 2026-11-21
---

# Pre-merge independent review workflow

# 1. Purpose and Scope

Code-delivering tasks with `review.independent` enabled must be independently
reviewed on the live pull request and its exact full 40-character head SHA
**before** the pull request is merged.

Previously, a `pr_merged` task under `delivery.auto_merge` or `review.acceptance`
off had a circular completion gate: completion refused an open PR and demanded a
merged PR before `on_parent_complete` could spawn a `code_review` child. As a
result, PR #450 merged before independent review; a P2 finding required follow-up
PR #451, and later review rounds kept inspecting #450's stale head.

In scope: breaking the `pr_merged` completion circularity when independent review
is enabled; gating merge authorization and merge helpers on an independent review
approval bound to the exact PR identity and exact full head SHA; cross-difficulty
review chain fallback with actionable blocked decisions; idempotent reviewer
spawning and concurrent settlement; and blocking acceptance of stale approvals
across head changes or replacement PRs.

# 2. Non-Goals

- No new external service or database table; all state stays in existing SQLite
  tables (`tasks`, `events`, `decisions`, `settings`, `model_executions`). The only
  migration updates the `code_review` kind's input and result schemas (3.7).
- No change to tasks when `review.independent.mode` is `off` or for non-code
  tasks.
- No bypass or weakening of existing merge grants (`delivery.auto_merge`,
  `input.delivery.allow_merge`, `repository:write`, `ARGUS_MERGE_AUTHORIZED`),
  branch protections, CI gates, or log redaction.
- No invented model IDs and no fallback to the implementer's model or provider.

# 3. Decisions Made

## 3.1 Non-circular `pr_merged` completion

When `review.independent.mode` is not `off` for a task's project (and the task is
not itself a `code_review` child), `deliverable.check_complete` does not require
`PrState.MERGED` before the independent review runs:

- A `pr_merged` task under `delivery.auto_merge` or `review.acceptance` off
  passes completion when the forge confirms the PR is `open` with exact-head
  checks `success` (or already `merged`). Closed or unreadable PRs, or non-green
  checks, still fail closed with `422 deliverable_missing`.
- Completion then enters `in_review` through `on_parent_complete`, which resolves
  the live PR URL and exact full head SHA (preferring the live forge read over
  older attempt artifacts) and spawns the read-only `code_review` child in a
  separate session.
- After independent review approves that exact PR and head, the authorized merge
  path merges the PR at that exact head SHA, and the forge confirms the merged
  state via `review_settle` or the review decision accept gate.
- When `review.independent.mode` is `off`, `pr_merged` completion is unchanged.

## 3.2 Exact PR and head SHA binding

- Every `review.independent_passed` event records both `pr` and `head_sha`
  alongside `child_id` and `verdict`.
- `reviewed_approval_of` reads the newest review gate lifecycle event on the
  parent (`passed`, `void`, `changes_requested`, `requested`, `waived`,
  `child_failed`) and returns `(pr, head_sha)` only when that newest event is
  `review.independent_passed`. A void, new round, or `request_changes` invalidates
  earlier approvals.
- `on_review_child_complete` compares the child's reported `head_sha` against the
  child's assigned `head_sha`, the parent's current delivery refs, and the live
  forge read (when available), and compares the assigned PR identity against the
  parent's current PR and live forge PR. Any PR number change (`pr_mismatch`) or
  head SHA change (`head_mismatch`) voids the verdict (`review.independent_void`)
  and spawns a fresh reviewer for the current live PR and head.
- `forge_verdict` and `settle_or_wait` verify both PR identity (`pr_moved`) and
  exact head SHA (`head_moved`) before accepting a task.
- Operator or orchestrator acceptance (`tasks.review`) checks
  `check_accept_reviewed` when `review.independent` is active and not explicitly
  waived (`approve_without_review` or a manual-fallback gate decision): an
  approval for PR A at head X is refused (`409 independent_review_stale` or
  `independent_review_required`) if the forge shows replacement PR B or head Y.
- `_delivery_refs` and `delivery.task_reported_head` pick the newest PR artifact
  by `observed_at` and never copy an older PR's `head_sha` onto a replacement PR.
  Current-attempt `result.data`, current-attempt `progress_data`, and live forge
  reads override older attempt artifacts so retry rounds always inspect the new
  head.

## 3.3 Pre-merge gate and merge authorization

- `independent_review.merge_gate_status` evaluates whether a merge is permitted
  for `(parent, pr, head_sha)`: it allows merge only when `review.independent` is
  `off`, explicitly waived, or the latest review event is an independent approval
  whose child's recorded executions pass `child_models_clean` and whose `pr` and
  exact 40-character `head_sha` match the target PR and head with no active
  reviewer or blocking gate reason.
- `ClaimedTask` carries `independent_review_required`,
  `independent_review_approved_pr`, and `independent_review_approved_head`;
  `readiness.decide_merge` and `argus-merge-auth.ts` (`taskMayMerge`) deny merge
  when `independent_review_required` is true and no approval exists, while keeping
  all existing grants (`auto_merge`, `allow_merge`, `repository:write`,
  `prompt_trust`) unchanged.
- When `independent_review_required` is true and an approval exists,
  `scopeMergeAuthorization` binds the approved PR number (`ARGUS_MERGE_APPROVED_PR`)
  and exact 40-character head SHA (`ARGUS_MERGE_APPROVED_SHA`) into the per-attempt
  HMAC (`ARGUS_MERGE_AUTHORIZED`). `gitea-api.mjs` (`requireMergeAuthorization`)
  and `argus-task.mjs` (`mergePr`) refuse any merge for a different PR number or
  different live head SHA before issuing a forge write.

## 3.4 Review chain precedence and blocked decision

- Precedence 1: a configured `review.chains` entry for the parent's difficulty is
  used first, unchanged.
- Precedence 2: when `review.chains` has no entry for the parent's difficulty,
  Argus searches other difficulties in `review.chains` (sorted difficulty names,
  then chain order), followed by `execution.chains` (sorted difficulty names,
  then chain order), for alternatives that differ from both the author model and
  the author provider and have a usable account (and an active compatible worker
  when workers are registered).
- Precedence 3: only when neither `review.chains` nor `execution.chains` configures
  any difficulty does Argus fall back to legacy `pick_reviewer` over
  `model-policy.json`.
- Argus never invents model IDs and never falls back to an author model. When no
  eligible reviewer exists, the parent stays `in_review` with
  `block_reason: no_independent_reviewer` and one actionable `review` decision
  (`payload: {gate: "review_failed", reason: "no_independent_reviewer", failed}`)
  offering `retry_reviewer`, `approve_without_review`, and `cancel`.
- `retry_reviewer` resolves `(pr, head_sha)` from the parent's delivery record or
  fresh forge read even when no prior `code_review` child was created.

## 3.5 Idempotency and concurrency

- `spawn_reviewer` checks active `code_review` children of the parent: if an
  active child already targets the same `(pr, head_sha)`, it returns that child ID
  without creating a duplicate. If an older active child targets a stale head or
  PR, it spawns the new child first and then cancels the stale child so
  `on_review_child_ended` ignores the superseded child.
- `on_review_child_complete` is idempotent per `child_id` and ignores late
  verdicts from superseded (non-latest) reviewer children.
- `retry_reviewer` returns an already-active reviewer child instead of spawning a
  duplicate.
- `review_settle.settle_task` re-checks state, `AWAITING_FORGE`, active reviewer,
  open decisions, and `(reviewed_pr, reviewed_head)` inside the single writer
  transaction so concurrent settlements produce at most one accept or one
  re-review.

## 3.6 Worker merge continuation

Argus never merges. After an approval, `review_settle` (the existing sweep, one write
transaction, forge read just before it) requeues an approved `pr_merged` parent
(`in_review`, `awaiting_forge`) for one worker attempt through the `reject` transition
and records `review.merge_resumed {pr, head_sha}`. All of these must hold, re-read
inside the transaction:

- the newest review gate event is `review.independent_passed` for exactly the live PR
  and head, and `merge_gate_status` allows it; no active reviewer, no open review
  decision;
- the forge shows the PR `open`, fresh, on the approved head, with exact-head checks
  green, mergeable and the forge review policy met (`readiness.pr_gate`);
- the task's own grant allows merge (`decide_merge`: `delivery.auto_merge` or
  `input.delivery.allow_merge`, and `repository:write`), read live, so a revoked grant
  resumes nothing.

When only the grant is missing, the parent keeps waiting and records
`review.merge_resume_blocked {reason}` once (`merge_not_authorized` or
`repository_write_missing`); it never bypasses a gate. A moved head or replaced PR is
voided and reviewed again by `settle_or_wait`, not resumed.

Worker contract: the continuation claim carries `independent_review_approved_pr` and
`independent_review_approved_head`, so the per-attempt authorization is scoped to them,
and the task spec gains a "Merge continuation" section telling the worker to change no
code and run `argus-task merge --pr N --sha <head>`, which fences the live PR and head
immediately before the one forge write and reads the merge back. A worker without that
helper cannot merge, ends the attempt unmerged and gets the decision below.

Idempotency: one resume per approval (a resume event newer than the approval for the
same PR and head), so duplicate, concurrent or restarted sweeps find nothing to do.
When the continuation attempt completes, `on_parent_complete` does not review the
approved head again: the parent settles on the forge's merged state, and when the PR is
still open it opens one orchestrator review decision (merge it and accept, or reject to
try again). A rejected review, failed CI or a new approval cycle starts from the start.
Review-disabled and non-code tasks never take this path.

## 3.7 Reviewer fixes its findings

An independent reviewer that finds actionable problems may fix them itself instead of
sending the parent back for a full attempt.

- The `code_review` child is created with `repository:write` and input
  `fix: {pr, base_head_sha, force_push: false}`. Its deliverable is a response with no
  merge grant, so it can push to the PR's own branch and nothing else: one normal
  fast-forward commit with tests, never a force-push, history rewrite, other branch or
  merge (the kind schema rejects `force_push: true`; `force_push` stays `no`).
- The runtime enforces that scope: the channel exports `ARGUS_FIX_PR` and
  `ARGUS_FIX_BASE_SHA` from `input.fix` (a malformed `fix` exports a marker that fails
  closed). Both names cross the host → session boundary through the container and Codex
  env allowlists (`patch-merge-auth-env.py`, `install.sh`) and are cleared when no fix
  scope is active, so a session without a fix scope never inherits one. `argus-task
  deliver` then takes the fix path only. It requires the PR open
  on the current branch, `HEAD` strictly ahead of the live PR head, which must be the
  reviewed head or a descendant of it, runs the pre-push checks and pushes without
  `--force-with-lease`. It never rebases, never pushes another branch and never creates or
  edits the PR; a moved or diverged head is refused with `request_changes` as the action.
- Fixer model exclusion applies to account-sourced selectors and to the legacy picker
  (used when no execution chain resolves) in every review mode, including
  `different_provider`, so a respawn cannot pick the fixer's model through an account
  whose configured model it is, nor select it again from the chain. With no eligible
  model left the parent blocks with `no_independent_reviewer`.
- Verdicts: `approve` (no findings, nothing changed), `request_changes` (cannot fix
  safely, nothing changed; the existing handoff to the implementer), `fixed` with
  `head_sha` (reviewed), `fixed_head_sha` (full 40-character new head), `findings` and
  `tests`.
- On `fixed`, in one write transaction and after the usual model-proof check of the
  reviewer: the live forge read (always taken for a fixing child) must show the same PR
  at exactly `fixed_head_sha`, which differs from the reviewed head, and the reviewer's
  recorded executions must name exactly one model. Otherwise the verdict is void
  (`fix_head_invalid`, `fix_unverified`, `fix_pr_mismatch`, `fix_head_mismatch`,
  `fix_provenance_unclear`) and a manual review decides; nothing is approved and no
  reviewer is respawned automatically.
- A verified fix records `review.independent_fix_pushed {child_id, pr, old_head_sha,
  new_head_sha, fix_models, fix_providers, findings, tests}`. As the newest lifecycle
  event it invalidates every older approval.
- The fix author is an author of the code: `fixers()` adds its models to the exclusion
  set of the next review (in every mode, by model name), and `child_models_clean`
  rejects any approval by the fixing child or by a child running a fix author's model.
  Chain precedence (3.4) is unchanged, so the fresh review runs on a different model in a
  separate session on the exact new head; with no eligible model the parent stays blocked
  (`no_independent_reviewer`, one actionable decision) and no merge is allowed.
- A fix spends one review round like `request_changes`; at `max_rounds` the parent opens
  one escalation decision and starts no reviewer. Replays of the same child are ignored
  (`_child_already_processed`) and the spawn is idempotent per `(pr, head)`.
- A head change during review or merge is handled as before: the approval is void
  (`head_mismatch`) and the merge fence (3.3) refuses any other head.

# 4. Open Questions

None.

# 5. Constraints

- Single-node SQLite only; no new migration, table, or external service.
- Forge reads stay outside `Database.write()` transactions; all writes use
  `tx.now`.
- English only across specs, code, comments, and tests.

# 6. Implementation Consequences

- `apps/api/src/argus_api/db/migrations/0088_code_review_fix.sql`: `code_review` schemas.
- `packages/core/src/argus_core/tasks.py`, `delivery.py`: additive fields on
  `ClaimedTask` and `IndependentReviewSummary`.
- `apps/api/src/argus_api/tasks/merge_resume.py`, `independent_review.py`, `deliverable.py`,
  `delivery.py`, `worker.py`, `pr_binding.py`, `review_settle.py`, `manage.py`,
  `readiness.py`, `views.py`, `routes.py`, `decisions/service.py`.
- `integrations/nanoclaw/argus-merge-auth.ts`, `gitea-api.mjs`, `argus-task.mjs`,
  `argus.ts`, `patch-merge-auth-env.py`, `install.sh`.
- Regenerated `packages/api-client/openapi.json` and
  `packages/api-client/src/schema.d.ts`.
- Living spec updates in `docs/specs/settings.spec.md` (3.5) and
  `docs/specs/task-deliverable.spec.md` (3.4, 3.5).

## Verification boundary

- Commands:
  - `uv run pytest -q -p no:cacheprovider apps/api/tests/test_review_fix.py apps/api/tests/test_pre_merge_independent_review.py apps/api/tests/test_review_settle.py apps/api/tests/test_execution_routes.py apps/api/tests/test_settings.py apps/api/tests/test_task_deliverable.py apps/api/tests/test_task_readiness.py`
  - `node --test integrations/nanoclaw/argus-task-merge.test.mjs integrations/nanoclaw/argus-merge-auth-env.test.mjs`
  - `uv run ruff check` / `ruff format --check` / `uv run ty check`
  - `python3 scripts/check_specs.py && python3 scripts/validate_docs.py && python3 scripts/validate_language.py && python3 scripts/check_paths.py`
- Verified: deterministic end-to-end pre-merge workflow against in-memory SQLite
  and mocked forge transport, cross-difficulty chain precedence, #450/#451
  replacement PR regression, idempotent retries and concurrent settlements, and
  Node/CLI authorized merge path checks.
- Not verified: live GitHub/Gitea forge calls, real LLM model sessions, or
  production deployment.
