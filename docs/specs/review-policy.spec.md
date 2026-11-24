---
status: implemented
created: 2026-11-24
updated: 2026-11-24
---

# Review policy: review tiers by changed path, observed before enforced

# 1. Purpose and Scope

`review.independent` is on or off per project ([settings](settings.spec.md) 3.5):
every code task gets the same independent review, whatever it changed. Between
2026-10-16 and 2026-11-23, 150 of 827 commits on `main` (18%) touched only docs or
tests, and about 220 (27%) touched paths where a mistake costs more: migrations,
auth, merge and review gates, leases, CI (a rough path match). A docs-only change
pays for a full review round, and a migration gets no more scrutiny than a style fix.

This spec adds a path-based review policy per project. It sorts each delivered head
into a tier (`skip`, `standard` or `human`) and has an observe mode that records what
the policy would decide without changing anything, so the rules can be checked
against real review outcomes before they are enforced.

In scope: the setting, reading a PR's changed paths, evaluating the tier, what each
tier changes when enforced, the observe record and the review report.

# 2. Non-Goals

- No model calls in the evaluation: rules are path globs that Argus evaluates
  deterministically. Natural-language rules are an open question.
- No policy file in the repository. A worker can push to the repository it works on
  and must not be able to edit its own review policy, so the policy is an admin
  setting.
- No use of worker-reported changes ([task changes](task-changes.spec.md)): the
  worker is the party under review. The forge's file list at the exact head is the
  only source.
- No effect when `review.independent.mode` is `off`, or on tasks that deliver no
  code.
- No change to forge branch protection, CI or merge grants. `skip` never grants a
  merge the task did not already have.
- No new table or migration.

# 3. Decisions Made

## 3.1 Setting

Key `review.policy`, scopes `global` and `project` (the project value wins), admin
only:

```json
{
  "mode": "off",
  "default": "standard",
  "rules": [
    { "name": "docs", "tier": "skip", "paths": ["docs/**", "**/*.md"] },
    { "name": "schema", "tier": "human", "paths": ["apps/api/src/argus_api/db/migrations/**"] }
  ]
}
```

- `mode`: `off` (default, nothing is evaluated), `observe` (evaluate and record,
  change nothing) or `enforce`.
- `default`: the tier of a changed path that no rule matches, `standard` or `human`.
  It is never `skip`, so an unlisted path is never waved through.
- `rules`: at most 50. `name` is unique, 1 to 40 characters of `[a-z0-9-]`; `tier` is
  `skip`, `standard` or `human`; `paths` holds 1 to 50 globs.
- Globs match repository-relative POSIX paths, case-sensitive: `*` matches within one
  path segment, `**` matches zero or more segments, `?` matches one character. There
  is no negation and no character class. A malformed value is a 422 naming the field.

## 3.2 Changed paths

Before a reviewer is spawned for a head (`spawn_reviewer`, for every new head: the
first completion, a rework, a repair, a reviewer fix), Argus reads the PR's changed
files from the forge, outside the write, with the repository account and budgets of
[task delivery](task-delivery.spec.md) 3.2: `GET .../pulls/{n}/files` on GitHub and
Gitea, 100 per page, at most 300 files. A renamed file contributes both its new and
its previous path.

The list is **unknown** when the read fails or is partial, when the PR has more than
300 files, or when the PR head the forge reports is not the head being evaluated.

## 3.3 Evaluation

Tiers are ordered `skip < standard < human`. Each changed path takes the strictest
tier among the rules that match it, or `default` when none does; the head takes the
strictest tier over its paths. A head is `skip` only when every path matches a `skip`
rule and no stricter one, so one migration file makes it `human`. An unknown or
empty list takes `default`.

Each evaluation records `review.policy_evaluated {mode, tier, pr, head_sha, files,
matched, unmatched, reason}` on the task: `matched` is `[{rule, tier, count}]`,
`unmatched` at most 20 paths, `reason` is `files_unknown` when the list was unknown.
A settings change applies from the next evaluation.

## 3.4 Enforce

- **`standard`**: today's independent review, unchanged.
- **`skip`**: no reviewer is spawned for that head. Argus records
  `review.policy_skipped {pr, head_sha, rules}`, a review gate lifecycle event bound
  to that exact PR and head. `reviewed_approval_of` and `merge_gate_status` treat it
  like an approval of that head, so the parent waits in `awaiting_forge`, then settles
  or resumes the merge as after an approval (pre-merge 3.1, 3.6). A newer head voids
  it like it voids an approval and is evaluated again. Acceptance settings
  (`review.acceptance`, the task's `review`) still apply.
- **`human`**: the independent review runs as today. After it approves, the parent
  neither settles nor resumes the merge by itself: it opens one review decision for a
  person (payload `{gate: "policy_human", rules, pr, head_sha}`), and the merge
  continuation (pre-merge 3.6) is never started for that head. The person merges on
  the forge and accepts, or rejects. `accept` still needs the forge to show the
  deliverable met and the approval at that head.

## 3.5 Observe and the review report

In `observe` mode Argus records `review.policy_evaluated` and changes nothing else:
every head is reviewed as it is today.

`GET /api/projects/{slug}/review-report?days=30` (admin, default 30 days, at most
90) returns a `policy` section: for each tier the policy chose, the evaluated heads
and tasks, the review verdicts on those heads (`approve`, `request_changes`, `fixed`,
`void`), their findings by priority, and how the tasks ended (succeeded, rejected by
a person, cancelled). It also lists every `skip` head whose review found a P1 or P2
problem (task, PR, rule, finding count): the signal that a rule is too wide. The
Settings page shows the report under the `review.policy` editor for the selected
project. [Review finding verification](review-finding-verification.spec.md) adds a
`findings` section to the same report.

There is no MCP tool for the report: it is an operator's tuning view, and the
orchestrator does not act on it. The setting itself is reachable through the existing
`get_settings` and `set_setting` tools.

The task's Reviews section (pre-merge 3.8) shows the tier of each reviewed head and
the rules that set it, in both modes.

## 3.6 Trust

- Only an admin sets the key. A worker or task input cannot set, widen or lower a
  tier; there is no task-level override.
- The changed-file list comes only from the forge at the exact head, never from task
  text, worker progress or the task's change snapshot.
- An unknown list is never `skip`.
- A `skip` binds to one head; any new push is evaluated again.

# 4. Open Questions

- Should `skip` also cap the size of the change (for example 200 changed lines), so a
  large docs-only PR is still reviewed?
- Are natural-language rules worth a model call per head, for what globs cannot
  express (for example "changes a public API")?
- Should the policy also apply with independent review off, so `human` can require a
  person on sensitive paths anyway?
- Should there be a lighter review level between `skip` and `standard`, such as the
  cheapest eligible reviewer?

# 5. Constraints

- Single node: one more bounded forge read per evaluated head, outside every write;
  the evaluation is in process; events and settings only, no table or migration.
- Read-only toward the forge, with the existing repository credentials.
- English only.

# 6. Implementation Consequences

- `apps/api/src/argus_api/settings/service.py`: the `review.policy` key and its model.
- New `apps/api/src/argus_api/tasks/review_policy.py`: the glob matcher, evaluation
  and the report query.
- `apps/api/src/argus_api/tasks/delivery.py` (`PrChecker`): the PR files read with its
  caps.
- `apps/api/src/argus_api/tasks/independent_review.py`: evaluate in `spawn_reviewer`;
  `review.policy_skipped` in `reviewed_approval_of` and `merge_gate_status`; the
  `human` gate where an approved parent would settle or resume the merge; the claim's
  approval fields (`ClaimedTask`) read a skip like an approval.
- `packages/core/src/argus_core/decisions.py`: gate `policy_human`.
- The report route, API models and `make openapi`; dashboard Settings and Reviews.
- [Settings](settings.spec.md) 3.5a and [pre-merge independent
  review](pre-merge-independent-review.spec.md) 3.2, 3.3 and 3.6 describe the key and
  the skip approval.
- `review_rounds.py`: `ReviewRounds.policy` carries each head's tier for the Reviews
  section; the Settings page shows the report (`views/ReviewPolicy.tsx`).

## Verification boundary

- Commands: `uv run pytest apps/api/tests/test_settings.py
  apps/api/tests/test_pre_merge_independent_review.py
  apps/api/tests/test_review_settle.py apps/api/tests/test_task_delivery.py`;
  `make openapi-check`.
- Tests (`apps/api/tests/test_review_policy.py`, `test_task_delivery.py`,
  `test_settings.py`): glob semantics (`*`, `**`, `?`, renames); strictest tier wins;
  unknown and empty lists take `default`; `default` refuses `skip`; observe changes
  nothing; a `skip` allows the merge of exactly its head and is voided by a new head;
  `human` never starts the merge continuation.
- Verified: the suites above, with the forge files endpoint mocked for renames, paging
  to exactly 300 files, more than 300, a moved head and a failed read.
- Not verified: the files endpoint against a live GitHub or Gitea. A task whose PR the
  worker did not report (found by search) has no forge reference, so its list is
  unknown and `default` applies; it is never skipped.
