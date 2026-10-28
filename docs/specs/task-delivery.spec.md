# Task delivery

# 1. Purpose and Scope

A worker delivers through a pull request and may be merged long before its task
moves: PR #1116 merged while its task stayed claimed with progress saying the PR was
open. Argus must **show what the forge says** about the task's PR so an
orchestrator can reconcile, without deciding anything on its behalf.

In scope: finding the one unambiguous PR a task refers to; reading its state from
Gitea, read-only; exposing it on REST, MCP and the dashboard; flagging a merged PR
for review and offering the existing accept only where the existing rules allow.

# 2. Non-Goals

- No auto-close, no attempt spent, no state change: **merged is not accepted**
  ([task continuation](task-continuation.spec.md) 3.1, 3.5). Delivery and CI stay the
  NanoClaw worker's job; Argus observes.
- No new credential handling, service, table or migration. Nothing is persisted: the
  state is read on request and never stored.
- No free-text guessing of references, and no repository presumed for a project.
- No PR state on task lists (one forge call per row); detail and the delivery
  endpoint only.

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

The proof of a repository is the pools' explicit `nanoclaw.repositories` map
(project -> HTTPS owner/repository). A project that pools disagree on has none.
Argus tasks are never assumed to live in Radar's repository. Restricting URLs to
that allowlist also means the credential is never sent to a host from task text.

## 3.2 Reading the state

`PrChecker` calls `GET /api/v1/repos/{owner}/{repo}/pulls/{n}` with the registered
`gitea` repository account (the existing SOPS-backed credential, HTTP Basic as
for workers); no other credential path. Outside any write transaction, 5 s timeout,
no redirects, concurrent reads of one PR share one request, a result is reused for
15 s. `merged: true` is `merged` (with `merged_at`), else `open` or `closed`. A
missing credential, network error, timeout, non-200 or unparsable body is
**`unknown`** and never `open`.

## 3.3 Exposure

`GET /api/tasks/{id}/delivery` (same read scope as the task; workers only their
leased task) and MCP `get_task_delivery` return `null` or `Delivery`:
`pr` (URL), `pr_state` (`open|merged|closed|unknown`), `merged_at`, `checked_at`,
`flags`, `accept_decision_id`, `accept_blocked`. The dashboard task drawer shows
it; `unknown` has its own dashed badge ("unknown (not read)"), distinct from `open`.

## 3.4 Merged pending review

When `pr_state` is `merged`, `flags` contains `delivery_merged_pending_review`. An
accept is offered (`accept_decision_id`) only when the task is `in_review`, has an
open review decision and no pending checklist items; otherwise `accept_blocked` is
`not_in_review`, `checklist_pending` or `no_review`. Accepting is the existing
`POST /decisions/{id}/answer` with `choice: accept`, so ownership, human-review
and orchestrator rules apply unchanged and pending QA cannot be discarded.

# 4. Open Questions

- Persisting the observed state through `observe` (so it survives a forge outage and
  appears on lists/inbox) is deferred until the read-time view proves insufficient.
- Other forges: only Gitea and GitHub are read ([GitHub repositories](github-repositories.spec.md)).

# 5. Constraints

- Read-only; no write to the forge, no new required service, no migration.
- Never log or return the credential; failures log only the PR URL.
- English only.

# 6. Implementation Consequences

`argus_core.delivery` models, `argus_api.tasks.delivery` (parser, checker, accept
offer), the REST route, the MCP tool, `PrChecker` in app state, the dashboard
`delivery.tsx`, regenerated `openapi.json`/`schema.d.ts`, and tests for the parser,
checker, REST, MCP and UI.
