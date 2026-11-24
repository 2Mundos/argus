---
status: draft
created: 2026-11-24
updated: 2026-11-24
---

# Review finding verification: structured findings, checked before they cost a round

# 1. Purpose and Scope

An independent reviewer's `request_changes` sends the task back for a full attempt,
with its findings appended to the spec ([pre-merge independent
review](pre-merge-independent-review.spec.md)). Nothing checks a finding before it
costs that attempt, and the `code_review` result schema accepts any string or object
as a finding.

In the preview install, between 2026-10-16 and 2026-11-23, there were 63 review
children for 32 tasks: 26 `request_changes` and 23 `approve` verdicts; 8 P1, 25 P2
and 2 P3 findings; 6 tasks needed four or more review runs and 5 exhausted their
rounds. The findings of those 5 tasks are mostly specific and often reproduced, so
the reviewers do not look often wrong. The same read shows three problems a rule can
fix:

- two P1 findings with a priority and a location but an empty description;
- one finding returned in three consecutive rounds of one task, with no record of
  whether the implementer did not fix it or disagreed with it;
- one task with a new P2 edge case in each of five rounds.

So this spec does not assume reviewers are often wrong. It makes findings structured,
lets the implementer dispute a finding with evidence, and adds a verification pass
that starts in observe mode, so measured data decides whether verification should
ever gate a round.

In scope: the finding shape, disputes, repeats, the verifier and its modes, what the
implementer receives, events, the rounds view and the review report.

# 2. Non-Goals

- No change to how a reviewer is picked (pre-merge 3.4), or to `approve` and `fixed`
  verdicts.
- A verifier never approves: refuting every finding neither accepts the task nor
  allows a merge.
- No triage by priority (turning P2 findings into follow-up tasks); see Open
  Questions.
- No new table. The kind changes are one migration.

# 3. Decisions Made

## 3.1 Finding shape

With verdict `request_changes` or `fixed`, each finding is an object:

| Field | Rule |
| --- | --- |
| `id` | short slug, unique in the result (`f1`, `f2`, …) |
| `priority` | `P1`, `P2` or `P3` |
| `file` | repository-relative path |
| `lines` | optional, `N` or `N-M` |
| `title` | one line, 1 to 120 characters |
| `description` | 1 to 2000 characters |
| `evidence` | `{kind, detail}`: `kind` is `test` (a failing test or command and its output), `reproduction` (steps run and what was seen) or `reading` (code reasoning only); `detail` 1 to 2000 characters |
| `repeat_of` | optional, `<child id>:<finding id>` of an earlier round's finding raised again (3.3) |

A string finding, or a missing or empty field, is the existing 422 naming it.
`approve` keeps `findings: []`. Stored results are not rewritten. The reviewer's spec
states the shape and that P1 findings with only `reading` evidence are checked first
when verification is on (3.4).

## 3.2 Disputes

The **Review findings** section handed to the implementer lists each finding with its
id. The implementer's next completion reports, in `data.review_responses`, one entry
per finding: `{id, status: "fixed" | "disputed", evidence}`. `fixed` names the test or
check that shows it; `disputed` explains why the finding is wrong, with a test,
command output or code reference. A missing response is recorded as `unanswered`;
it does not refuse the completion.

The next reviewer receives the previous round's findings and the responses. It must
either drop a disputed finding or raise it again with `repeat_of` and new evidence.

## 3.3 Repeats

A finding with `repeat_of` is a repeat. The handoff marks it "raised again (round
N)". The rounds view and the review report count repeats per task and show whether
the earlier round's response was `fixed`, `disputed` or `unanswered`.

## 3.4 Verifier

Key `review.independent` ([settings](settings.spec.md) 3.5) gains `verify_findings`:
`off` (default), `observe` or `enforce`.

When it is on, each `request_changes` verdict gets a verifier: a child of the new
kind `review_verification` on the same PR and head, read-only (no
`repository:write`), with input `{review_child_id, pr, head_sha, findings}`. Its model
comes from the reviewer route (pre-merge 3.4) with the reviewer's model excluded as
well as the authors'. Its spec gives it the findings and any disputes and asks it to
try to refute each finding: run the evidence, read the code, and return
`verifications: [{id, status, evidence}]` with `status` `confirmed`, `refuted` or
`unclear`.

- **`observe`**: the verdict applies at once, as today. The verifier runs alongside
  it, and each answer records `review.finding_verified {review_child_id,
  verifier_id, id, priority, status}`. Nothing else changes.
- **`enforce`**: the verdict waits for the verifier. Then:
  - `confirmed` and `unclear` findings go to the implementer; a doubt never drops a
    finding;
  - `refuted` findings are not sent, and the round lists them with the refutation;
  - when every finding is refuted, Argus opens one review decision for a person
    (payload `{gate: "findings_refuted", review_child_id, verifier_id}`) showing both
    sides. `reject` sends the findings to the implementer anyway. `accept_with_deviation`
    with text waives the independent review, as for `rounds_exhausted` (pre-merge 3.2).
    Plain `accept` still needs an approval at that head.
- With no eligible verifier, or a verifier that fails or returns no answer, the
  original verdict applies unchanged and Argus records
  `review.finding_verification_skipped {reason}`.

A verifier costs one more read-only run per `request_changes` round, in `observe` as
in `enforce`.

## 3.5 Visibility

- `GET /api/tasks/{id}/reviews` (pre-merge 3.8) returns, per finding, its `id`,
  `title`, `evidence.kind`, `repeat_of`, the implementer's response and the
  verification status, and, per round, the verifier child. The dashboard Reviews tab
  shows them. `review_verification` children are hidden from the board like
  `code_review` children.
- The review report (`GET /api/projects/{slug}/review-report`, created by this spec
  or by [review policy](review-policy.spec.md), whichever is built first) gains a
  `findings` section: findings by priority and verification status; disputes and how
  the next round answered them; repeats; and, for refuted findings, whether a later
  round raised them again. That last number estimates how often a verifier would have
  dropped a real problem, and it is the evidence for moving from `observe` to
  `enforce`.

# 4. Open Questions

- When one task keeps producing new P2 edge cases, should rounds after the second send
  back only P1 findings and turn P2 and P3 into follow-up tasks?
- Should `observe` verify only P1 findings, to halve its cost?
- In `enforce`, should a refuted P1 ever be dropped without a person seeing it?

# 5. Constraints

- Single node: child tasks and events only; one migration for the kind schemas; no
  new table.
- The verifier is read-only and never runs an author's or the reviewer's model; model
  IDs are never invented (pre-merge 3.4).
- English only.

# 6. Implementation Consequences

- The next migration in `apps/api/src/argus_api/db/migrations/`: the `code_review`
  result schema (finding object, `repeat_of`) and the new `review_verification` kind.
- `apps/api/src/argus_api/tasks/independent_review.py`: the reviewer's spec text, the
  `verify_findings` field, spawning and completing the verifier, holding the verdict
  in `enforce`, and `_findings_text` with ids, responses and repeats.
- `apps/api/src/argus_api/tasks/review_rounds.py`, the API models and `make openapi`;
  dashboard `TaskReviews.tsx`; the board filter that hides review children.
- `packages/core/src/argus_core/decisions.py` and `decisions/service.py`: gate
  `findings_refuted` and its `accept_with_deviation` waiver.
- When built, update [pre-merge independent
  review](pre-merge-independent-review.spec.md) 3.2, 3.7 and 3.8 and
  [settings](settings.spec.md) 3.5.

## Verification boundary

- Commands (planned): `uv run pytest apps/api/tests/test_pre_merge_independent_review.py
  apps/api/tests/test_review_fix.py apps/api/tests/test_review_rounds_api.py
  apps/api/tests/test_settings.py`; `make openapi-check`.
- Tests to write: the finding schema (a string finding and an empty field are 422);
  `observe` applies the verdict unchanged; `enforce` sends only confirmed and unclear
  findings; all refuted opens one decision and never approves; a failed verifier
  leaves the verdict as it was; the verifier never uses the reviewer's or an author's
  model; repeats and responses reach the next reviewer.
- Verified: nothing yet (draft). The review counts come from a read-only query of the
  preview database on 2026-11-23. "Specific and often reproduced" is a reading of the
  finding text, not a check against the code.
- Not verified: how often a verifier refutes a real finding. `observe` exists to
  measure that.
