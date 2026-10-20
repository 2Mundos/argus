# Progress milestones

# 1. Purpose and Scope

A task card must show what actually happened, not what an agent last said. A free-text
note such as "Delegating implementation now" stays on the card for as long as the agent
is quiet, even when a commit and a pull request already exist. This spec adds
**milestones**: a closed set of structured progress signals taken from real sources
(git state, executor output, the Gitea commit-status API), carried by the existing
progress endpoint ([worker lifecycle](worker-lifecycle.spec.md) 3.12,
[live work](live-work.spec.md) 3.1), plus a dashboard indicator for a claimed task that
has gone quiet.

# 2. Non-Goals

- No new endpoint, table, or task state; no change to state semantics or to the lease
  rule (a progress post still renews the lease).
- No transcript or tool-call stream.
- No milestone history table: the latest milestone is on the task row, the history is
  the `task.progress` events.
- No change to `argus-delegate`'s JSON report (the last stdout line).

# 3. Decisions Made

## 3.1 Kinds and data (closed set)

| `kind` | `data` |
| --- | --- |
| `commit_created` | `branch` (string), `sha` (7-40 hex) |
| `pr_opened` | `number` (int >= 1), `url` (`http(s)`, no userinfo, query or fragment) |
| `ci_running`, `ci_passed`, `ci_failed` | `sha` (7-40 hex) |
| `gate_failed` | `gate` (string, at most 200 characters) |
| `waiting_input` | none |

Any other kind, or `data` that does not match its kind, is `422 validation_failed`.
`data` without `kind` is refused. Unknown keys are refused.

## 3.2 API

- `ProgressRequest` gains optional `kind` and `data`; `message` stays required (a
  readable line for clients that ignore milestones). MCP `report_progress` takes the
  same optional fields.
- `tasks.progress_kind` and `tasks.progress_data` (JSON text) are nullable. The task
  views expose `progress_kind` and `progress_data` (an object). The server adds `at`
  (its own time of the milestone) to `progress_data`, because later free-text notes
  move `progress_at`.
- A note **with** a kind sets the milestone, the message and `progress_at`. A note
  **without** a kind leaves the milestone as it is, so a PR link outlives chatter.
  Both are cleared where progress is cleared (retry).
- **Rate limit.** Free-text notes keep one per 10 s per task. A milestone is not
  limited when its kind differs from the stored `progress_kind`; the same kind again
  within 10 s is `429` like a note. A free-text note after a milestone is limited
  against `progress_at` as before.
- Old tasks have null kind and render as before.

## 3.3 Emitter (`argus-delegate`)

- A small pure module (`milestones.mjs`) with injectable probes (git, pull-request
  discovery, CI status). `argus-delegate` polls it every 15 s while the executor runs
  and once more when it exits.
- Sources: `HEAD` of `--cwd` differing from `HEAD` at start -> `commit_created`; an
  `http(s)` pull-request URL in the executor's output whose path is `/pulls/N` ->
  `pr_opened`; the commit-status API for the current `HEAD` through the credential-free
  gateway client (`gitea-api.mjs`), when `ARGUS_REPOSITORY_URL` is allowlisted ->
  `ci_running` (pending), `ci_passed` (success), `ci_failed` (failure, error), the
  last also naming each failing status context as `gate_failed`; `waiting_input` when
  the executor's final JSON reports permission denials. Each distinct milestone is
  emitted once. A probe that throws is ignored for that poll.
- Sinks: one JSON line on stderr and in `<out>/milestones.jsonl`; and a `POST
  /api/tasks/{id}/progress` when `ARGUS_API_URL`, `ARGUS_TASK_ID`,
  `ARGUS_TASK_ATTEMPT` and `ARGUS_AGENT_TOKEN` are all set. The token is the agent's own
  bearer token, the one the NanoClaw channel uses; it is never logged or written to
  `milestones.jsonl`. The channel does not put it into the agent container today, so
  until the pool environment does, only the file and stderr are written (4).
- Failures of a sink never fail the delegation.

## 3.4 Dashboard

- A task card and the activity header show the milestone label, short SHA, the PR as a
  link when present, and its relative time (`progress_data.at`).
- **No signal.** For a `claimed` task, when now minus `progress_at` exceeds
  `NO_SIGNAL_MINUTES` (default 10, overridable with `VITE_NO_SIGNAL_MINUTES`), the card
  says "no signal for N min" and the stale note is shown as the last known one, not as
  current. A claimed task that never sent progress has no `progress_at` and shows
  nothing extra.
- Tasks without a kind render as before.

# 4. Open Questions

- Delivering `ARGUS_AGENT_TOKEN` (or a narrower per-task token) to the agent container
  needs a pool or channel change; the channel could also tail `milestones.jsonl` itself.
- `ci_*` needs the repository to be allowlisted for the worker and a CI that posts
  commit statuses.

# 5. Constraints

- Single-node: no new service. The emitter reads local git and calls the Gitea API only
  through the gateway client.
- Never log or store credentials. PR URLs are sanitised before they are emitted.
- Migration `0019` is additive and nullable.

# 6. Implementation Consequences

One PR: this spec and its index link; migration `0019`, API, MCP and core model;
`milestones.mjs` and its wiring in `argus-delegate`; dashboard card and activity; tests
for each; regenerated `openapi.json` and `schema.d.ts`.
