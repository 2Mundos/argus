---
status: draft
created: 2026-11-21
updated: 2026-11-24
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
  `child_failed`, and `review.policy_skipped`) and returns `(pr, head_sha)` only when
  that newest event is `review.independent_passed` or `review.policy_skipped`: an
  enforced policy `skip` ([review policy](review-policy.spec.md) 3.4) is the approval
  of exactly that PR and head, with no reviewing child. A void, new round, or `request_changes` invalidates
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
- When independent review exhausts `max_rounds` with the newest review still
  requesting changes, the human review decision carries payload
  `{"gate": "rounds_exhausted", "child_id", "rounds"}` (decisions opened before the
  payload existed are matched by their `review.independent_escalated` event).
  `accept_with_deviation` on it needs text, waives the independent-review check and
  records `review.independent_waived` (`decision_id`, `notes`, unresolved `child_id`).
  Plain `accept` still requires an independent approval at the exact head; `reject`
  sends the task back for rework. No other decision kind gains this waiver.
- A `request_changes` verdict carries structured findings (`id`, `priority`, `file`,
  `title`, `description`, `evidence {kind, detail}`, optional `lines` and `repeat_of`);
  the kind schema answers anything else with a 422 and `approve` keeps `findings: []`.
  The implementer answers each finding in `data.review_responses`, and the next
  reviewer sees the earlier findings, responses and repeats ([review finding
  verification](review-finding-verification.spec.md) 3.1 to 3.3). With
  `verify_findings` on, a read-only `review_verification` child checks the findings
  first (3.4 there); an all-refuted round opens a review decision with payload
  `{"gate": "findings_refuted", review_child_id, verifier_id}`, where
  `accept_with_deviation` waives like `rounds_exhausted` and records
  `review.independent_waived` with `reason: findings_refuted`.

## 3.3 Pre-merge gate and merge authorization

- `independent_review.merge_gate_status` evaluates whether a merge is permitted
  for `(parent, pr, head_sha)`: it allows merge only when `review.independent` is
  `off`, explicitly waived, or the latest review event is an independent approval
  whose child's recorded executions pass `child_models_clean` (a policy `skip` has no
  child and needs no such proof) and whose `pr` and
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

- The reviewer uses the author's own `execution.chains`; there is no separate review
  chain. Argus starts at the `execution.chains` chain for the parent's difficulty
  (chain order), then continues through the other difficulties (sorted difficulty
  names, then chain order), and takes the first alternative that differs from the
  author in both model and model vendor, is not excluded (an alternative or account
  that already ended a review without a verdict), has a usable account, and has an
  active compatible worker when workers are registered.
- A paired alternative (harness + executor) is compared by its executor only: the
  harness merely coordinates, the executor is the model that reviews. Both selectors
  must still have a usable account. A single alternative is compared by its harness.
  The route's skipped entry for an author exclusion of a paired alternative carries
  `selector: executor`.
- A paired reviewer's verdict counts only when its executor ran: the review child's
  attempt must record a `succeeded` `delegate` model execution whose executor
  (adapter) and provider match the selected alternative's executor selector, and its
  model when the selector is explicit. Otherwise the verdict is void: the parent is
  neither accepted nor rejected, `review.independent_void` is recorded with
  `reason: executor_not_run`, and Argus selects again without that alternative (a
  fresh reviewer, or `no_independent_reviewer`). The child's spec tells the harness
  to run the review through `argus-delegate` and not to review the code itself.
  Single-alternative reviews are unchanged.
- The vendor is derived from the model, never from the account: an OpenRouter
  `vendor/model` id has that prefix as its vendor, and a native `claude-*`, `gpt-*` or
  `gemini-*` id is `anthropic`, `openai` or `google`. Routers and endpoints (`custom`
  accounts such as OpenRouter, `local`) are not vendors, so a `deepseek/...` model
  through OpenRouter may review an Anthropic author, `claude-sonnet-*` through
  OpenRouter may not, and two different models that share a `custom` router are not
  blocked by it. When the vendor cannot be derived it is unknown and is not excluded
  on provider alone; the model rule still applies.
- Only when `execution.chains` configures no difficulty at all does Argus fall back
  to legacy `pick_reviewer` over `model-policy.json`.
- Argus never invents model IDs and never falls back to an author model. When no
  eligible reviewer exists, the parent stays `in_review` with
  `block_reason: no_independent_reviewer` and one actionable `review` decision
  (`payload: {gate: "review_failed", reason: "no_independent_reviewer", failed}`)
  offering `retry_reviewer`, `approve_without_review`, and `cancel`.
- `retry_reviewer` resolves `(pr, head_sha)` from the parent's delivery record or
  fresh forge read even when no prior `code_review` child was created.
- The head a reviewer targets follows the parent's newest delivery. When a fix round
  lands in a follow-up PR (the original already merged), the PR the worker reported
  in this attempt (`result.data.pr`, a `pr_opened` progress, or the newest PR
  artifact, including one bound by `reconcile_task_pr`) wins over the older PR, and
  the forge head of the merged older PR is not used for it. When a reviewer ends
  without a verdict (`failed`, `cancelled`) or an operator retries, the next reviewer
  takes the parent's current delivery when it names a different PR with a head; the
  ended child's `(pr, head_sha)` is only the fallback when the parent has no
  different delivery. Exact-head voiding (3.2) and author exclusion are unchanged.

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

- the newest review gate event is `review.independent_passed` or
  `review.policy_skipped` for exactly the live PR and head, and `merge_gate_status` allows it; no active reviewer, no open review
  decision;
- the forge shows the PR `open`, fresh, on the approved head, with exact-head checks
  green, mergeable and the forge review policy met (`readiness.pr_gate`);
- the task's own grant allows merge (`decide_merge`: `delivery.auto_merge` or
  `input.delivery.allow_merge`, and `repository:write`), read live, so a revoked grant
  resumes nothing.

A head the enforced policy puts in the `human` tier is never resumed: after the
approval the parent opens one review decision with payload `{gate: "policy_human",
rules, pr, head_sha}` and a person merges and accepts, or rejects.

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

When the approved PR's checks fail, it stops being mergeable or it is closed, the sweep
does not wait on it: the [PR steward](pr-steward.spec.md) opens a decision, or, with
`review.independent.max_repairs` above 0, requeues the parent for one bounded repair
attempt. A repair request counts like a resume in the idempotency rule above (one per
approval, newer than the approval for the same PR and head). A repair that moves the
head is reviewed again from the start and is not a review round; one that does not
move it settles from the forge or opens the decision.

## 3.7 Reviewers are verdict-only

An independent reviewer judges the PR head it was given and never changes the PR.

- The `code_review` child has no `repository:write` capability and no `fix` input: it
  cannot push to the PR branch and cannot create or close a PR, and its deliverable is a
  response with no merge grant. The instructions say so ("verdict-only: no commit, no
  push, no PR create or close") and tell it to hand fixes to the implementer through a
  `request_changes` verdict with findings.
- Verdicts: `approve` (no findings) and `request_changes` (the existing handoff to the
  implementer). A `fixed` verdict is not accepted: it is void (`fix_not_permitted`), a
  manual review decides, nothing is approved and no reviewer is respawned automatically.
- If the PR head differs from the reviewed head when the verdict arrives (anything moved
  the branch during the review), the verdict is void (`head_mismatch`) and a fresh review
  of the new head is spawned, as in 3.2. The merge fence (3.3) refuses any other head.
- Output and tests are bounded. The reviewer reads the forge with the compact default of
  `argus-github-api` (never `--raw`, [GitHub repositories](github-repositories.spec.md)
  3.4) and runs only the tests that cover the diff, each under `timeout 300`; it never
  runs a whole test suite. A reviewer that cannot finish within its context reports what
  it checked instead of exhausting it.
- A reviewer whose login is rejected before any model ran does not count a failure: the
  claim is voided and requeued, leaves no unsaved-work block, and the next eligible
  chain serves the review ([failure classes](failure-classes.spec.md) 3.2).
- A review spends one round like `request_changes`; at `max_rounds` the parent opens one
  escalation decision and starts no reviewer. Replays of the same child are ignored
  (`_child_already_processed`) and the spawn is idempotent per `(pr, head)`.

A reviewer's reply whose `result.data` fails the kind's schema (a finding without
`description`, say) is never lost: the API's 422 `validation_failed` carries a bounded
`details` list (`<path>: <message>`, at most 20 of 300 characters). The channel stores the
raw rejected reply as the artifact `rejected-result-attempt-N.txt` (at most 64 KiB), shows
the reviewer the fields in its session and lets it deliver once more in the same attempt.
A second rejection fails the attempt as `channel_error` with the `details` on
`POST /tasks/{id}/fail`: they land in the error text, the `task.fail` event data
(`details`) and the task log (`[reply rejected] …`), so the next attempt and the
orchestrator see which field was wrong.

## 3.8 Review rounds on the dashboard

Each review round stays a `code_review` child task; the board does not show them. The
dashboard board asks `GET /api/tasks` with `hide_review_children=true`, which the server
applies before paging (so totals and cursors stay correct) by leaving out `code_review` and
`review_verification` tasks that have a `parent_id`. A parent with rounds then carries `review_badge
{round, verdict, state}` for its newest round, shown on its card (for example `review 3 ·
changes requested`). The flag is off by default, so the MCP `list_tasks` and other callers
still see review children; a review child without a parent stays on the board, and the
inbox and `q` search never hide review children.

`GET /api/tasks/{id}/reviews` lists a task's rounds, oldest first (round 1), read-only and
scoped like the task: child id, state, verdict (`approve`, `request_changes`, `void`, or
null when the child ended without one), reviewed `head_sha`, findings (id, priority, title, file,
lines, description, evidence kind, `repeat_of`, the implementer's response and the
verification status) with counts by priority, the round's verifier child and its state,
the number of repeats, reviewer provider and model, report artifact
ids, `created_at` and `finished_at` (the child's last update once it has ended). It does
not change how reviewers are spawned, picked, counted or completed.

Task detail shows a Reviews section with one tab per round, the newest selected, each
with its verdict, reviewed head, findings, a link to the child task and its report
artifacts. A `code_review` task's own page still opens by URL and links back to its parent.

# 4. Open Questions

None.

# 5. Constraints

- Single-node SQLite only; no new migration, table, or external service.
- Forge reads stay outside `Database.write()` transactions; all writes use
  `tx.now`.
- English only across specs, code, comments, and tests.

# 6. Implementation Consequences

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
  - `uv run pytest -q -p no:cacheprovider apps/api/tests/test_review_fix.py apps/api/tests/test_codex_failover.py apps/api/tests/test_pre_merge_independent_review.py apps/api/tests/test_review_settle.py apps/api/tests/test_execution_routes.py apps/api/tests/test_settings.py apps/api/tests/test_task_deliverable.py apps/api/tests/test_task_readiness.py`
  - `node --test integrations/nanoclaw/github-api.test.mjs integrations/nanoclaw/argus-task-merge.test.mjs integrations/nanoclaw/argus-merge-auth-env.test.mjs`
  - `uv run ruff check` / `ruff format --check` / `uv run ty check`
  - `python3 scripts/check_specs.py && python3 scripts/validate_docs.py && python3 scripts/validate_language.py && python3 scripts/check_paths.py`
- Verified: deterministic end-to-end pre-merge workflow against in-memory SQLite
  and mocked forge transport, cross-difficulty chain precedence, #450/#451
  replacement PR regression, idempotent retries and concurrent settlements, and
  Node/CLI authorized merge path checks.
- Not verified: live GitHub/Gitea forge calls, real LLM model sessions, or
  production deployment.
