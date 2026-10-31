# Task discovery and prerequisite evidence

# 1. Purpose and Scope

An orchestrator converting a product spec into work must be able to:

1. **Find existing tasks** that already cover that work, with citations of where
   the text matched and which task version was read.
2. **See whether a task's dependencies are actually delivered** — forge-merged
   on an expected base, or a listed artifact re-verified on disk — distinguished
   from the dependency merely having `state == succeeded`.

In scope: a bounded text filter on the existing task list read; a read-only
prerequisite evidence view over direct `depends_on` edges; permissions and
freshness identical to existing task and delivery reads. Both surfaces are
REST and MCP.

# 2. Non-Goals

- No duplicate-detection heuristic and no claim that a text match means the work
  is already covered.
- No autonomous task creation or closure; no state transitions of any kind.
- No change to `depends_on` write semantics or readiness
  ([task dependencies](task-dependencies.spec.md)): a dependency still unblocks
  when it `succeeded`, regardless of forge or artifact evidence.
- No enforcing merged/verified delivery as a claim gate (`enforced: false`).
- No semantic, vector, stemming, or ranking search — only literal substrings.
- No new table, migration, required service, forge write, or dashboard change.

# 3. Decisions Made

## 3.1 Bounded task search

`GET /api/tasks` and MCP `list_tasks` gain an optional `q` query parameter.
When `q` is absent the response shape, totals and cursors are unchanged.

- **Parse.** Strip; split on whitespace into at most 8 terms; each term ≤ 64
  characters. Empty after strip → `422 query_empty`. Too many terms or a term
  too long → `422 query_too_broad`. Validation follows the existing
  `ValidationFailedError` style (`detail.code`).
- **Match.** Case-insensitive substring of **every** term over `title` and
  `spec` only. It is deliberately literal (no word boundaries, stemming or
  ranking) so it cannot be mistaken for semantic search. A term that appears
  only in `input`, `result`, progress or events does not match. Text match is
  never treated as duplicate detection.
- **Compose.** Existing filters (`state` multi, `project`, `kind`, `assignee`,
  `archived`, `view`, `limit`) and the worker lease scope compose with `q`. The
  same WHERE (including `worker_task_filter`) drives both `items` and `total`,
  so a worker's `total` counts only leased tasks; out-of-scope tasks are
  invisible, never filtered client-side.
- **Pagination.** Exact `total`; keyset cursor on `(created_at, id)` via
  `argus_api.paging`. The cursor fingerprint includes `q` and every other
  filter; reusing a cursor with different filters is `400 cursor_mismatch`.
- **Citations.** When `q` is set, each item carries `matches`: a list of
  `{field: "title"|"spec", snippet, truncated: bool}` for each matching field.
  Snippets are ≤ 200 characters, centred on the first hit, with ellipses when
  cut (`truncated: true`). Citation identity is the item's existing `id` and
  `version` (task `updated_at`). When `q` is absent, `matches` is omitted.
- **Empty vs truncated vs failed.** No matches → `items: []`, `total: 0` (not
  an error). Snippet cuts set `matches[].truncated` (and may appear in the
  row's `truncated` map). `omitted` keeps its projection meaning. A failed
  read is an HTTP error with a code; it is never returned as an empty page.

## 3.2 Prerequisite evidence view

`GET /api/tasks/{id}/prerequisites?expected_base=` and MCP
`get_task_prerequisites(task_id, expected_base=None)` (audience: anyone who may
read the task, same as `get_task_delivery`).

Response shape:

```
{task_id, version, enforced: false, expected_base, items, omitted, partial}
```

- **Edges.** Direct `depends_on` only (`task_deps`), ordered by dependency id.
  Cap `MAX_PREREQUISITES = 25` items; over-cap sets `omitted: ["dependencies"]`
  and `partial: true` — never silently drop.
- **Access.** `require_task_readable` (unknown task → 404; worker not leased →
  `403 not_leased`). Read connection only; no write transaction; no forge write.
- **Per item.**
  `{task_id, title, state, task_succeeded, verdict, why, evidence}` where
  `task_succeeded` is `state == "succeeded"` only and is **never** folded into
  `verdict`.
- **Evidence.** Reuses `argus_api.tasks.delivery.reference` + `PrChecker`:
  `pr`, `pr_state` (`open|merged|closed|unknown`), `merged_at`, `base_ref`,
  `head_sha`, `checked_at`, `source: "forge"`, plus
  `artifacts: [{id, name, sha256, size, verified, checked_at}]` for up to three
  file artifacts listed on the dependency's `result.artifacts` (deterministic
  id order). Re-hash is capped (8 MiB total across those artifacts); over the
  cap a size-only check is reported and `verified` stays `unknown` with the
  reason named — do not call unbounded `artifacts.service.verify`.
- **Verdict rules** (never infer from a matching title, a generic read,
  `task_succeeded`, or a closed PR alone):

  | Condition | `verdict` | `why` |
  | --- | --- | --- |
  | Forge `merged` and (`expected_base` absent or equals `base_ref`) | `satisfied` | `merged` (note: review state is not delivery proof; pending reviews still `satisfied`) |
  | Forge `merged` but `base_ref` ≠ `expected_base` | `unsatisfied` | `base_mismatch` |
  | Forge `closed` and not merged | `unsatisfied` | `closed_without_merge` |
  | Forge `open` | `unsatisfied` | `open` |
  | Forge unreadable (no credential, timeout, non-200, unparsable) | `unknown` | `forge_unavailable` (failed sub-read named in `partial`) |
  | A listed artifact re-hashes equal (`verified: true`) | `satisfied` | `artifact_verified` (wins alongside or instead of a PR) |
  | Listed artifact missing on disk | `unsatisfied` | `artifact_missing` |
  | Listed artifact size/hash mismatch | `unsatisfied` | `artifact_corrupted` |
  | Artifact unreadable / over byte cap without hash proof | `unknown` | `artifact_unverified` |
  | No PR reference and no listed file artifact | `unknown` | `no_reference` |

  When both a PR and artifacts are present, `satisfied` if either channel
  proves delivery; otherwise the most specific unsatisfied/unknown why from
  the PR channel is preferred when a PR exists, else the artifact why.
- **`enforced: false`.** This view never blocks claim or readiness. Whether a
  dependent should require merged/verified delivery rather than only
  `succeeded` is an open product policy (see §4); this surface documents
  evidence only.
- **`PrChecker`.** Extended so `base_ref` and `head_sha` are available from the
  same forge read. `check()` keeps its existing signature, 15 s cache and
  in-flight coalescing; richer callers share that cache so one PR is still
  read once per window ([task delivery](task-delivery.spec.md) 3.2).

## 3.3 Permissions and freshness

- Task search and the prerequisites view use the same read scope as a task
  read: managers are global; workers see only leased tasks
  (`require_task_readable` / `worker_task_filter`).
- Forge facts carry `checked_at` (wall time of the forge read). Argus facts
  carry the dependency task's `version` (`updated_at`) on the parent response
  and each item's state/title from that snapshot.
- PrChecker's existing 15 s reuse is the freshness bound for forge fields;
  artifact `checked_at` is the time of the bounded re-hash attempt.

## 3.4 Unresolved product policy (explicit)

Whether a dependent's claim or readiness should require merged-on-base or
artifact-verified evidence rather than only `succeeded` is **not decided**.
This view reports `enforced: false` and does not invent that policy. Any
future enforcement needs its own spec change and must not silently alter
`depends_on` semantics.

# 4. Open Questions

- Should readiness or claim require `verdict == satisfied` for every direct
  dependency? Deferred; see §3.4 (`enforced: false`).
- Persisting prerequisite snapshots (so a forge outage does not flip rows to
  `unknown`) — deferred until the read-time view proves insufficient, same
  posture as [task delivery](task-delivery.spec.md) §4.

# 5. Constraints

- Lightweight single-node: SQLite, no new required service, no migration.
- Read-only: no forge write, no autonomous create/close, no state machine change.
- English only. Negentropy: extend the existing list and delivery interfaces;
  do not fork a parallel search endpoint.
- Coordinate with delivery through `reference` + `PrChecker` only.

# 6. Implementation Consequences

1. This spec; index link; cross-links from
   [task dependencies](task-dependencies.spec.md) and
   [MCP operations](mcp-operations.spec.md).
2. `q` on `list_tasks` (views + REST + MCP), match snippets, cursor fingerprint.
3. `GET /api/tasks/{id}/prerequisites` + MCP `get_task_prerequisites`; SQL read
   of `task_deps`; bounded artifact re-hash; `PrChecker` detail fields.
4. Regenerated `openapi.json` / `schema.d.ts`.
5. Tests: `test_task_search.py`, `test_task_prerequisites.py` (fake HTTP /
   fixture only); existing delivery tests remain green.

## Verification boundary

- **Commands:**
  `uv run pytest -q apps/api/tests/test_task_search.py apps/api/tests/test_task_prerequisites.py apps/api/tests/test_task_delivery.py apps/api/tests/test_tasks_api.py apps/api/tests/test_mcp.py`;
  `python3 scripts/validate_docs.py`; `make openapi-check`;
  `uv run ruff check .`; `uv run ty check`.
- **Verified:** query/filter/pagination/permission/snippet semantics against
  SQLite fixtures; prerequisite verdicts against a mock `httpx` transport and
  local artifact files; OpenAPI schema drift; docs index linkage.
- **Not verified:** live forge credentials or network; production merge or
  deploy; dashboard UI; any claim that text match equals duplicate work; any
  enforcement of merged delivery on readiness.
