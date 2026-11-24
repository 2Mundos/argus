---
status: implemented
created: 2026-11-24
updated: 2026-11-24
---

# PR steward: keep an approved pull request moving

# 1. Purpose and Scope

With independent review on, an approved `pr_merged` or `pr_green` task waits in
`in_review` with `block_reason: awaiting_forge` until the forge shows its deliverable
met ([pre-merge independent review](pre-merge-independent-review.spec.md) 3.1, 3.6).
The settle sweep (`review_settle`) only waits. When the approved PR's checks fail, it
stops being mergeable, or it is closed, nothing repairs it and nobody is asked: the
task waits until a person notices. On 2026-11-21 task `bba2950d` waited about two
hours on a closed PR until it was cancelled. The startup repair
(`backfill_manual_reviews`) skips `awaiting_forge` on purpose, so a restart does not
help either.

This spec gives that wait a way out:

- a closed PR, or a PR that cannot become ready without a change, opens a decision
  for a person at once;
- when the project allows it, a failed check or a merge conflict on the approved head
  gets a bounded repair attempt by a worker;
- every push Argus causes without a person (a repair here, a reviewer fix of
  pre-merge 3.7) leaves a note on the PR with the old and new head and how to undo it.

In scope: classifying the forge read the sweep already takes, the repair attempt and
its cap, the decision, the undo note, events and the dashboard.

# 2. Non-Goals

- No forge events or webhooks: the sweep keeps polling ([single-node](single-node.spec.md)).
  No new loop, service, table or migration.
- Argus itself still never writes to the forge. Repairs and notes are made by a
  worker through `argus-task`.
- No repair of a head that was never approved, of a check that is still running or
  unread, or of a forge review requirement (`review_required`): those wait as today,
  with their reason visible.
- No reuse of an approval: a repaired head is a new head and is reviewed again.
- No change when `review.independent.mode` is `off`, for tasks that deliver no code,
  or for `code_review` children.
- No commands in PR comments (`@argus ...`): PR text is untrusted input, and
  steering stays in Argus ([task steering](task-steering.spec.md)).
- No automatic re-run of CI.

# 3. Decisions Made

## 3.1 What the sweep classifies

For a `review_settle` candidate whose newest review gate event is
`review.independent_passed`, the forge read `settle_task` already takes (fresh within
`FORGE_FRESH_S`) is classified with `readiness.pr_gate`:

| Forge on the approved PR and head | Class | Action |
| --- | --- | --- |
| `merged`, or `open` and ready | none | settle or resume the merge, as today |
| `closed`, not merged | `pr_closed` | decision (3.3) |
| `open`, reason `ci_failed` (a check failed, errored or was cancelled) | `ci_failed` | repair (3.2) or decision (3.3) |
| `open`, reason `not_mergeable` | `not_mergeable` | repair (3.2) or decision (3.3) |
| `open`, reasons only among `ci_running`, `ci_missing`, `ci_unknown`, `review_required`; or the read failed or was partial | none | keep waiting, as today |
| head or PR moved | none | void and review again, as today |

`ci_failed` and `not_mergeable` can hold together; the repair names both.

## 3.2 Repair attempt

Key `review.independent` ([settings](settings.spec.md) 3.5) gains `max_repairs`, an
integer from 0 to 3, default 0. At 0 there are no repairs and `ci_failed` or
`not_mergeable` opens the decision of 3.3.

Above 0, the sweep requeues the parent for one worker attempt when all of these hold,
re-read inside the write transaction:

- the class is `ci_failed` or `not_mergeable` on exactly the approved PR and head;
- there is no active reviewer and no open review decision;
- no repair was requested for this approval yet (at most one per approval);
- the task has fewer than `max_repairs` repair requests in total, across approvals,
  so a cycle of approve, break and repair cannot loop.

Otherwise the decision of 3.3 opens.

The requeue uses the `reject` transition the merge continuation uses, so attempt and
budget limits apply unchanged (a spent budget blocks the task, [task
cost](task-cost.spec.md) 3.6). It records `review.steward_repair_requested {pr,
head_sha, classes, failed_checks}` (`failed_checks`: at most 20 check names from the
delivery projection). The task spec gains a **PR repair** section that names the PR,
the head, the failed checks or the conflict with the base, and tells the worker:

- change only what makes those checks pass or the branch merge cleanly; add no scope;
- read failures with `argus-task ci-digest --sha <head>`; for a conflict, rebase onto
  the base and resolve it;
- push through `argus-task deliver --undo-note "<reason>"` (3.4);
- when the failure is not caused by the change (for example an unrelated flaky
  check), change nothing and say so in the completion.

When the repair attempt completes:

- **The head moved.** Argus records `review.steward_repair_pushed {pr, old_head_sha,
  new_head_sha}` and the existing completion path reviews the new head from the
  start. The merge fence (pre-merge 3.3) already refuses to merge a head that is not
  approved. A repair is not a review round and does not count against `max_rounds`.
- **The head did not move.** The same head is not reviewed again: a repair request
  counts like a merge resume in `resumed_for_approval`, so the parent settles from
  the forge read or, when the class still holds, opens the decision of 3.3.

A repair attempt that fails follows the normal failure and retry path. The repairing
model becomes an author through its recorded executions, so it cannot review its own
repair.

## 3.3 Decision when the steward stops

On `pr_closed`, or on `ci_failed` or `not_mergeable` with no repair allowed, the sweep
clears `awaiting_forge` and opens one review decision for a person: payload `{gate:
"pr_not_ready", class, pr, head_sha, failed_checks}`, a question naming the class,
and the regular review options. `accept` still needs the forge to show the
deliverable met and the independent approval at that head, so it works once the
person fixes or merges the PR on the forge. `reject` sends the task back with the
person's text, and the task can be cancelled. This gate does not waive the
independent review. Argus records `review.steward_stopped {class, pr, head_sha,
decision_id}`; while that decision is open, a later pass opens no other.

## 3.4 Undo note on automated pushes

`argus-task deliver` gains `--undo-note "<reason>"` (one line, at most 200
characters). When the push moves the head of an existing open PR, it posts one PR
comment after the push with the reason, the old and new head, and the command that
restores the old head:

- after a fast-forward push: `git revert --no-edit <old>..<new>`, then push;
- after a rebase pushed with lease:
  `git push --force-with-lease=<branch>:<new> origin <old>:refs/heads/<branch>`.

It posts nothing when the head did not move or the PR is not open. A failed comment is
reported in the helper's output (`undo_note: "failed"`) and never fails the delivery.
The PR repair section (3.2) and the reviewer fix instructions (pre-merge 3.7) tell
the worker to pass it.

## 3.5 Visibility

- The steward events (`review.steward_repair_requested`,
  `review.steward_repair_pushed`, `review.steward_stopped`) appear in the task's
  event timeline on REST, MCP and the dashboard like other task events.
- The task's Reviews section (pre-merge 3.8) shows the newest steward state between
  rounds: repair requested (class and failed checks), repair pushed (old and new
  head), or stopped (class and a link to the decision).
- The Settings page shows `max_repairs` as **Repair an approved PR that breaks
  (attempts)**.

# 4. Open Questions

- A wait that never changes, such as an unreadable forge or a check that stays
  pending for hours, still has no way out. Should the same `why` for 24 hours open the
  decision of 3.3?
- Should a repair first re-run a failed check once, to tell a flaky check from a real
  failure? That needs a forge write the helpers do not have.

# 5. Constraints

- Single node: the existing sweep and its budgets (`MAX_PER_PASS`, backoff). Forge
  reads stay outside the write; every gate is read again inside it, with `tx.now`.
- Argus stays read-only toward the forge; the worker writes through `argus-task` with
  its existing repository credential and the merge fence of pre-merge 3.3.
- Every way in has a way out: `max_repairs: 0` turns repairs off, the decision offers
  `reject`, and the task can be cancelled.
- English only.

# 6. Implementation Consequences

- `apps/api/src/argus_api/tasks/review_settle.py`: classify the read and route to
  repair or decision.
- `apps/api/src/argus_api/tasks/merge_resume.py` (or a `steward.py` beside it): the
  repair request, the stop decision, and `resumed_for_approval` counting a repair.
- `apps/api/src/argus_api/tasks/independent_review.py`: `max_repairs` on
  `IndependentReviewSetting`; `on_parent_complete` records
  `review.steward_repair_pushed`.
- `packages/core/src/argus_core/decisions.py`: gate `pr_not_ready`.
- `integrations/nanoclaw/argus-task.mjs`: `deliver --undo-note`; PR comment create in
  `github-api.mjs` and `gitea-api.mjs` if missing.
- Dashboard: the Reviews section and the settings field; `make openapi` if the
  setting model is on the wire.
- Kept in step: [settings](settings.spec.md) 3.5, [pre-merge independent
  review](pre-merge-independent-review.spec.md) 3.6 and 3.7, and [task
  helpers](task-helpers.spec.md) (`--undo-note`).

## Verification boundary

- Commands: `uv run pytest apps/api/tests/test_review_settle.py
  apps/api/tests/test_pre_merge_independent_review.py apps/api/tests/test_settings.py`;
  `node --test integrations/nanoclaw/argus-task.test.mjs
  integrations/nanoclaw/github-api.test.mjs integrations/nanoclaw/gitea-api.test.mjs`;
  `make openapi-check`.
- Tests: each row of the 3.1 table; one repair per approval and the total
  cap; a spent budget; a repair that pushes nothing; one open decision per stop; a
  head that moves between the read and the write; the undo note after a fast-forward
  and after a rebase.
- Verified: the 3.1 rows, one repair per approval and the total cap, a repair that
  pushes and one that pushes nothing, one decision per stop, a head moving between
  the read and the write (`test_review_settle.py`); the `max_repairs` range and
  default (`test_settings.py`); the undo note after a fast-forward and after a
  rebase, and when the head did not move, the PR is closed or the comment fails
  (`argus-task.test.mjs`). `max_repairs` ships at 0; no live repair has run.
- Not verified: live GitHub and Gitea comment writes.
