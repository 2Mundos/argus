---
status: implemented
created: 2026-10-24
updated: 2026-11-06
---

# Task delivery

# 1. Purpose and Scope

A worker delivers through a pull request and may be merged long before its task
moves: PR #1116 merged while its task stayed claimed with progress saying the PR was
open. Argus must **show what the forge says** about the task's PR so an
orchestrator can reconcile, without deciding anything on its behalf.

In scope: finding the one unambiguous PR a task refers to; reading its state,
exact-head checks/statuses, mergeability and review signals from Gitea or GitHub,
read-only; projecting that together with Argus checklist and authorization facts on
REST, MCP and the dashboard; flagging a merged PR for review and offering the
existing accept only where the existing rules allow. The same projection is the
evidence call an orchestrator uses when working an inbox `review` row
([MCP operations](mcp-operations.spec.md) 3.2c).

# 2. Non-Goals

- No auto-close, no attempt spent: **merged is not accepted**
  ([task continuation](task-continuation.spec.md) 3.1, 3.5). The projection itself
  changes no state; the one explicit writer is the reconciler of 3.7, which only
  closes a superseded merge question. Delivery and CI stay the
  NanoClaw worker's job; Argus observes. The one place the projection gates
  anything is accepting a task whose [deliverable](task-deliverable.spec.md) is
  `pr_green` or `pr_merged`: the accept reads it first and is refused short of
  that level. The projection itself still decides nothing.
- No implication that green checks authorize merge, or that merge authorizes accept.
  Authorization (task/policy `allow_merge`, accept offer) is a separate projection
  from forge CI and from checklist evidence.
- No new credential handling, service, table or migration. The projection stores
  nothing: the state is read on request. 3.7 records what it observed as an event
  and a continuation artifact.
- No free-text guessing of references, and no repository presumed for a project.
- No PR / CI state on task lists or inbox rows (one or more forge calls per row);
  detail and the delivery endpoint only. Inbox lists stay cheap; the orchestrator
  calls `get_task_delivery` for the selected review.

# 3. Decisions Made

## 3.1 Reference extraction

`tasks.delivery.extract` returns one PR or nothing, from the task's result
(summary and string values of `data`), progress (message and data), the last 50
events, and the continuation's artifacts:

- A **PR artifact** (kind `pr`, recorded by the continuation contract) wins; the
  newest observation is used. Its `url` or, for a bare numeric `ref`, the project's
  repository.
- A **full PR URL** (`https://host/owner/repo/pull|pulls/N`) counts only when
  host/owner/repo is a configured repository.
- `PR #N` / `pull request #N` counts only when the **project proves its repository**.
  No other `#N`, "PR 12" or issue form is a reference.
- Several different PRs in text with no artifact are ambiguous: none is chosen.

The proof of a repository is the project's own `repository_url`
([knowledge](knowledge.spec.md) 3.1), else the pools' explicit
`nanoclaw.repositories` map (project -> HTTPS owner/repository). A project that
pools disagree on, and has no record of its own, has none.
Argus tasks are never assumed to live in Radar's repository. Restricting URLs to
that allowlist also means the credential is never sent to a host from task text.

## 3.2 Reading the forge (exact head)

`PrChecker` reads with the registered repository account of the PR's provider
(`gitea` or `github`; existing SOPS-backed credential; Basic for Gitea, Bearer for
GitHub); no other credential path. Outside any write transaction, overall budget
**8 s**, no redirects, concurrent reads of one PR share one in-flight fetch, a
full projection is reused for **15 s**.

For one PR it may issue, in order, bounded GETs:

1. `.../pulls/{n}` — state, `merged` / `merged_at`, head and base (`sha`, `ref`),
   `mergeable` / `mergeable_state` when the forge returns them.
2. Commit statuses for the **exact head SHA** (Gitea
   `/commits/{sha}/statuses` or GitHub combined `/commits/{sha}/status` plus
   statuses). Cap **50** contexts (latest per context).
3. On GitHub only, check runs for that SHA (`/commits/{sha}/check-runs`), cap **50**.
4. Required status contexts **only when the forge discloses them** without elevating
   privilege: GitHub protected-branch `required_status_checks.contexts` /
   `checks` for the base ref when the credential can read it; Gitea branch
   protection `status_check_contexts` when readable. A 403/404/missing field means
   required gates are **unknown** (`null`), never an empty list that implies "none
   required".
5. PR reviews list, cap **20**, kept only as counts and whether any review's
   `commit_id` matches the exact head (no review body text returned).

`merged: true` is `merged` (with `merged_at`), else `open` or `closed`. A missing
credential, network error, timeout, non-200 or unparsable body on the **PR** read is
**`pr_state: unknown`** and never `open`. A failure on a **later** read (statuses,
check runs, protection, reviews) leaves `pr_state` from the PR body and records the
failed part in `partial` (for example `statuses`, `check_runs`, `required_gates`,
`reviews`); those fields stay unknown / empty as defined below, never invented green.

Check / status states normalize to
`success | pending | failure | error | skipped | cancelled | unknown`. Skipped is
not success. Combined over the exact head: `failure`/`error`/`cancelled` win, else
`pending`, else `success` when every known item is success or skipped, else
`unknown` when nothing was read or the statuses read failed.

## 3.3 Projection (REST / MCP / dashboard)

`GET /api/tasks/{id}/delivery` (same read scope as the task; workers only their
leased task) and MCP `get_task_delivery` return `null` or `Delivery`. Existing
top-level fields stay for clients already shipped:

`pr`, `pr_state` (`open|merged|closed|unknown`), `merged_at`, `checked_at`,
`flags`, `accept_decision_id`, `accept_blocked`.

Additive fields (ignored by old clients):

| Field | Meaning |
| --- | --- |
| `task` | `{id, state, project}` from Argus (source `task`) |
| `head` / `base` | `{sha, ref, source, checked_at}`; forge wins when the PR read succeeded; else task-reported head from continuation/result when present (`source: task`) |
| `merge` | `{mergeable, mergeable_state, source, checked_at}` from the PR body; unknown when absent |
| `checks` | bounded list of `{name, state, kind, sha, source, required, description?, target_url?, checked_at}`; `kind` is `status` or `check_run`; `required` is true/false only when required gates are known, else `null` |
| `checks_combined` | combined state over `checks` for the exact head, or `unknown` |
| `required_gates` | `null` when unknown; else `{contexts, source, checked_at}` (contexts may be empty only if the forge said so) |
| `review` | review **evidence** only: independent-review child summary from Argus when present; forge review counts (total / matching head); `agentic_finding_count` (`null` = unknown, never coerced to 0); each sub-fact carries `source` and `checked_at` / sha when forge-backed |
| `checklist` | `{pending, done, total, pending_ids}` from the continuation contract (source `task`); separate from forge and from authorization |
| `authorization` | `{allow_merge, accept_decision_id, accept_blocked, notes}`; `allow_merge` is true only when the task input explicitly sets `delivery.allow_merge` to boolean `true`, else false/unknown per input; notes state that green checks do not authorize merge and merge is not acceptance |
| `stale` | true when a task-reported head SHA disagrees with the forge head, or a returned check's `sha` is not the forge head |
| `partial` | names of forge sub-reads that failed or were truncated (`statuses`, `check_runs`, `required_gates`, `reviews`, `truncated_checks`, …) |
| `disclosures` | short English caveats always including that observation is read-only; when `partial` or `stale`, say so |

Every external (forge) fact is attributed with `source: "forge"` and the **exact SHA**
it was read for, plus `checked_at` of this projection. Argus-only facts use
`source: "task"`. Nothing is labelled success when unread.

## 3.4 Merged pending review

When `pr_state` is `merged`, `flags` contains `delivery_merged_pending_review`. An
accept is offered (`accept_decision_id`, also mirrored under `authorization`) only
when the task is `in_review`, has an open review decision and no pending checklist
items; otherwise `accept_blocked` is `not_in_review`, `checklist_pending` or
`no_review`. Accepting is the existing `POST /decisions/{id}/answer` with
`choice: accept`, so ownership, human-review and orchestrator rules apply unchanged
and pending QA cannot be discarded. A task that failed `deliverable_missing` while
its PR later merged shows `not_in_review` until a manager reconciles the delivery
([deliverable](task-deliverable.spec.md) 3.8), which opens the review decision and
nothing more; the checklist still blocks the offer.

## 3.5 Inbox coordination

`GET /api/inbox` / `get_inbox` do **not** embed delivery or CI (3.2 / non-goals).
For an inbox row with `decision_kind: review` (or `in_review` with no decision yet),
the actionable evidence path is: `get_decision` → `get_task_delivery` → continuation
/ artifacts as today. The orchestrator skill names `get_task_delivery` in the
review steps. MCP tool description states the same.

## 3.6 Privacy and budgets

- Never log or return the credential; failures log only the PR URL.
- No review or comment bodies, no job logs, no patches in this projection.
- Status descriptions and URLs truncated (description ≤ 200 characters).
- Caps in 3.2; over-cap sets the matching `partial` token (`truncated_checks`,
  `truncated_reviews`).
- Cache and coalescing as in 3.2; overall wall budget 8 s.

## 3.7 A merge elsewhere closes the merge question

Argus receives no forge events, so a worker that asked for its PR to be merged
(`ASSIST: pr-merge: <url>`, the typed resource `pr-merge`) stays blocked on that
question when a person or bot merges the PR: no holder is left to `observe`, and
the projection above is read-only by design. `decisions.merge_supersede` is the
explicit seam: a background loop (`ARGUS_MERGE_SUPERSEDE_INTERVAL_S`, default
60 s, `0` turns it off), not a GET, not the MCP tool.

Each pass looks at up to 200 blocked tasks and reads the forge for at most 10 of
them, outside any write; a task whose read did not change anything waits 60 s,
doubling to 15 min (in memory, so a restart starts over within the same bound).
A task is **eligible** only when all of these hold, checked before the read and
again inside the write that acts on it:

- it is `blocked`, not archived, and its only open decision is a `question` with
  `category: assistance`, resource `pr-merge`, audience orchestrator, asked by a
  worker, and its block reason is exactly `waiting for decision <id>`;
- the project proves its repository (3.1), the task has one unambiguous PR, and
  every PR artifact is that PR in that repository (no bare number under a foreign
  URL, no second PR);
- the forge read of that PR (3.2) is `merged` with head, merge commit and
  `merged_at`, observed within the last 2 minutes, and a head the task reported
  is the forge head.

Anything else, including an unknown, stale, moved, mismatched or ambiguous
observation or a forge failure, changes nothing. A historical question with free
text (`merge the PR`, no typed resource), another resource, a human audience or an
additional open question is left for the orchestrator, with one
`task.merge_blocker_skipped` event naming the reason and decision (again only
when the reason changes). Terminal, claimed and ready tasks are not candidates, so
nothing is resurrected, no attempt or failure count changes and a task out of
attempts or budget keeps its own block.

When eligible, **one write** answers the question as `system:merge-supersede`
through the ordinary answer path (so the task unblocks and the answer is an
effective decision), records the merged PR as a continuation artifact (provenance
`argus_event`, the forge's head and observation time), sets the explicit phase
`close_evidence` with an objective carrying repository, PR, head and merge commit
and forbidding another merge, a duplicate PR, reimplementation and reverting newer
base changes, and writes `task.merge_blocker_superseded` (source, repository, PR,
head, merge commit, `merged_at`, `checked_at`, the pending checklist ids and what
is **not** asserted). Replays, concurrent answers and claims find the question
closed and write nothing more.

The task then goes through the normal claim by any worker, whether or not the
former one or its session still exists, and ends only with a structured
`complete` carrying per-criterion evidence. Argus does not complete, accept or
tick anything, and the merge does not assert that `pr-ready`, checks, review or
acceptance passed; the checklist, parser discrepancies and deviations stay as
recorded for the worker and the review to settle.

# 4. Open Questions

- Persisting every observed state (so it survives a forge outage and appears on
  lists/inbox) is deferred; only the merge of 3.7 is stored.
- Other forges: only Gitea and GitHub are read ([GitHub repositories](github-repositories.spec.md)).
- Whether agentic-review comment parsing belongs in Argus or stays worker-side
  (`review-findings`); this projection may leave `agentic_finding_count` unknown.

# 5. Constraints

- Read-only toward the forge; no new required service, no migration. The reconciler
  reads the forge outside every write and re-checks inside it.
- Never log or return the credential; failures log only the PR URL.
- English only.
- Single-node: in-process checker, no broker.

# 6. Implementation Consequences

`decisions.merge_supersede` and its loop in `main.py` (3.7), the NanoClaw
`close_evidence` phase rule, `argus_core.delivery` models (additive fields), `argus_api.tasks.delivery` (parser,
checker fetching PR + exact-head statuses/checks/optional protection/reviews,
accept offer, checklist/authorization assembly), the REST route, the MCP tool
description, `PrChecker` in app state, the dashboard `delivery.tsx` (show head,
combined checks, stale/partial, keep accept),
[MCP operations](mcp-operations.spec.md) 3.2c, orchestrator skill review steps,
regenerated `openapi.json`/`schema.d.ts`, and tests for the parser, checker
(partial failure, required-gates unknown vs empty, stale head, budgets), REST,
MCP and UI.

## Verification boundary

- Commands: `uv run pytest apps/api/tests/test_task_delivery.py
  apps/api/tests/test_merge_supersede.py`;
  `pnpm --filter @argus/dashboard exec vitest run src/delivery.test.tsx`;
  `make openapi-check`.
- Verified: parser, mock-transport forge reads (Gitea/GitHub), partial failure,
  required-gates unknown vs empty, REST/MCP projection and accept offer, dashboard
  badges for unknown/stale/partial — fixture and fake HTTP only.
- Not verified: live Gitea/GitHub against real credentials or branch-protection
  shapes beyond the mocked paths; agentic `finding_count` parsing remains unknown.
