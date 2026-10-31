# Task resumability preflight

# 1. Purpose and Scope

When an orchestrator is about to answer, requeue or release a task so that a new
attempt can start elsewhere, it needs an honest, bounded answer to: *what is
actually true about this task's state, its WIP and its artifacts, and what is
merely recorded?*

Observed failure modes this closes:

- A receipt that once said `verified` is not current availability of the bytes.
- A local workspace path or a checkpoint id is not a transfer to another attempt.
- A merged PR is not acceptance ([task continuation](task-continuation.spec.md)).
- Unknown is not absent: missing observation is not proof that nothing exists.

This spec adds a **read-only resumability preflight**: one REST endpoint and one
MCP tool that assess the current attempt's lease, continuation, published versus
local WIP, and attachment availability through the existing artifact handoff
([task inputs](task-inputs.spec.md)). It never mutates, never enforces, and never
opens a second transport.

# 2. Non-Goals

- No mutation: no event, no row update, no artifact write, no lease change.
- No enforcement and no blocking: existing claim, answer, requeue and release
  behaviour stays exactly as it is. Do **not** add a gate or any new
  `ENV_BLOCK_REASONS`. Future enforcement needs a separate authoritative policy
  and a TOCTOU-safe transaction; this preflight alone does not prevent a race
  between assessment and the next write.
- No new transport: reuse the task-inputs attachment store,
  `GET /tasks/{id}/inputs`, `GET /artifacts/{id}/content`, and NanoClaw
  `materializeTaskInputs`. Local paths are not assumed portable.
- No new table or migration.
- Argus does not drive Git or forges; remote branch/PR/commit artifacts are not
  re-read here.
- No "actionable inbox" or "decision preview" contract: neither is defined on
  current `main` (no matching living spec under `docs/specs/`). This preflight
  keeps to its own read-only surface and does not invent those contracts.
- No dashboard change and no deploy.

# 3. Decisions Made

## 3.1 The interface

`GET /api/tasks/{id}/resumability` and MCP `task_resumability_preflight`.

Access is the existing task read rule (`require_task_readable`): managers are
global; workers may only read tasks they hold or held a lease on
(`403 not_leased`). Unauthenticated calls are `401`; an unknown task is `404`.

Optional query parameters `expected_attempt` and `expected_version` (the task's
`version`, i.e. `updated_at`): a mismatch makes the response `stale` rather than
silently describing a newer state.

Read-only: the route opens `db.read()` only, writes no event, no row and no
artifact. Attachment bytes are hashed outside any write transaction.

## 3.2 Response shape

A bounded, typed report with no free content bytes:

| Field | Meaning |
| --- | --- |
| `task_id`, `attempt`, `task_version` | The attempt and version the report describes |
| `assessed_at` | Server time of the assessment |
| `decision` | `resumable` \| `not_resumable` \| `unknown` \| `stale` |
| `cause_codes` | Closed `ResumabilityCause` values that drove `decision` |
| `reasons` | Each: `code`, `subject`, `evidence_class`, bounded redacted `detail` |
| `artifacts[]` | Per known continuation / attachment artifact assessment |
| `wip` | Published / local_only / unknown assessment |
| `inputs[]` | Shared attachments with receipts, availability and consumer path |
| `lease` | Holder, expiry, and whether the lease is live for this attempt |
| `decision_changes[]` / `unknowns[]` | Decisions and gaps relative to the attempt |
| `next_steps[]` | Bounded, typed hints (not commands) |

Caps (hard): 20 artifacts, 20 inputs, 10 reasons, 5 next steps, free text at
300 characters, and the serialised response must stay small enough for MCP.

## 3.3 Evidence classes

Every claim carries exactly one evidence class:

| Class | Meaning |
| --- | --- |
| `recorded_metadata` | Argus stored it (row, continuation JSON, event) |
| `receipt_only` | A historical `task.input` receipt proves a past
  materialization, verification or consumption — nothing about now |
| `current_read` | This request re-read the stored bytes and compared size and
  SHA-256 |
| `unobserved` / `unknown` | Argus has no observation, or could not complete a
  current read |

Recorded metadata and historical receipts **never** imply current availability.
A preflight proves nothing about a different attempt than the one it read.

## 3.4 Availability verification for attachments

For artifacts with `source = attachment` (and only those) the preflight re-reads
the stored bytes through the existing local artifact store and hashes them —
reusing `argus_api.artifacts.service.verify` / `_path` / `key`, not a second
reader. Classification:

| Availability | When |
| --- | --- |
| `available` | Size and SHA-256 equal the recorded ones |
| `missing` | No file at the store key |
| `truncated` | Fewer bytes than the recorded size |
| `corrupted` | Size equals recorded but hash differs, or more bytes than recorded |
| `inaccessible` | Read raises `PermissionError` / `OSError` other than not-found |
| `unknown` | Larger than the preflight read bound, or any other error |

Bounds: do not read more than `min(recorded size, configured read bound)` plus
one extra byte to detect overflow; above the bound, availability is `unknown`.
Never return bytes, never a path outside the store, never a name the server
rejected.

A remote artifact (branch / PR / commit) is never read by the preflight: it is
`recorded_metadata` with availability `unknown` and cause
`artifact_state_unobserved`.

## 3.5 WIP

Derived from `continuation.wip` and artifacts:

- `published` when an artifact with provenance other than `worker_reported` and a
  non-`unknown` state covers it (branch / PR / commit with a head).
- `local_only` when the WIP ref is a local sha/path, or the ref is absent and
  there is no covering artifact.
- `unknown` when the ref names `checkpoint:<id>:<sha256>` — the checkpoint store
  is node-local and invisible to the server (`checkpoint_store_not_visible`).
- A WIP `attachment:<id>:<sha256>` reference is verified like 3.4.

Always add an explicit reason that a workspace path or a checkpoint id is **not**
a transfer to another attempt.

## 3.6 Inputs

For each shared attachment of the task (`list_inputs`):

- the recorded artifact metadata;
- its latest receipts per attempt/state as `receipt_only`;
- the current availability from 3.4;
- the supported consumer path: `task_inputs_materializer` (NanoClaw
  `materializeTaskInputs`, which needs the `task-inputs` capability — check the
  task's `required_capabilities` and report `capability_not_required` /
  `capability_unknown` when it does not advertise it) or `none`.

Do not add another consumer. Receipts stay history; they are never upgraded to
`current_read`.

## 3.7 Attempt and decision changes

Which decisions were decided at or after the attempt started, which are
unresolved, and which artifacts were observed after the attempt started. The
response says the preflight describes the attempt it read and is `stale` when the
caller's `expected_attempt` / `expected_version` no longer match. Reuse the
continuation view's `premise_outdated`, `gaps` and `interruption`.

## 3.8 Decision codes

Closed `ResumabilityCause` enum, at minimum:

`task_unknown`, `task_terminal`, `attempt_mismatch`, `stale_preflight`,
`no_artifacts`, `artifact_state_unobserved`, `artifact_unavailable`,
`artifact_missing`, `artifact_truncated`, `artifact_corrupted`,
`artifact_inaccessible`, `artifact_unknown`, `wip_local_only`,
`checkpoint_store_not_visible`, `wip_unrecorded`, `input_capability_missing`,
`decision_unresolved`, `acceptance_not_structured`, `lease_expired`,
`lease_held_elsewhere`.

`decision` is derived from them:

- `resumable` only when everything required is `current_read` / available and
  nothing required is missing;
- `not_resumable` when a required input is missing / corrupted / truncated /
  inaccessible, or the task is terminal;
- `unknown` when any required fact is unobserved;
- `stale` when the caller's premise does not match.

## 3.9 Secrets and bounds

Free text (artifact notes, WIP summary, decision questions) is redacted with
`argus_core.redact` and bounded before it leaves. No content bytes, no token, no
host path outside the artifact store, no receipt reporter detail beyond ids.

## 3.10 Authority and effects

The preflight grants nothing. `merge`, force-push, deploy and Production access
are unchanged ([prompt-data isolation](prompt-data-isolation.spec.md),
[task continuation 3.9](task-continuation.spec.md)). Its only effect is a read.

# 4. Open Questions

- Enforcement / TOCTOU: a separate authoritative policy that re-checks inside the
  write that answers, requeues or releases; this preflight alone cannot close the
  race.
- Whether remote forge observation (live PR/branch head) belongs on this surface
  or stays on `POST /observations`.
- Whether the checkpoint store should ever be readable by the server for
  cross-attempt transfer.
- Retention of attachment bytes versus receipt history.

# 5. Constraints

- Single node, SQLite reads only ([single-node](single-node.spec.md)).
- No network inside a write transaction; attachment bytes are hashed outside any
  write transaction.
- Bounded response (3.2); English only; tenant / project / task isolation via the
  existing read rule.

# 6. Implementation Consequences

- `docs/specs/task-resumability-preflight.spec.md` (this file); index and
  cross-links from [task continuation](task-continuation.spec.md) and
  [task inputs](task-inputs.spec.md).
- `packages/core/src/argus_core/resumability.py`: models and pure classification.
- `apps/api/src/argus_api/tasks/resumability.py`: `assess(...)`.
- `apps/api/src/argus_api/tasks/routes.py`: `GET /{task_id}/resumability`.
- `apps/api/src/argus_api/mcp_server.py`: `task_resumability_preflight`.
- Tests: `apps/api/tests/test_resumability_preflight.py`,
  `integrations/nanoclaw/argus-resumability.test.ts`.
- `make openapi` regenerates the client schema.

## Verification boundary

- Commands: `uv run pytest -q -p no:cacheprovider
  apps/api/tests/test_resumability_preflight.py`;
  `./apps/dashboard/node_modules/.bin/vitest run
  integrations/nanoclaw/argus-resumability.test.ts --root .`;
  `python3 scripts/validate_docs.py`.
- Verified: read-only assessment against real SQLite and the local artifact
  store; access boundaries; redaction; availability classes for shared
  attachments; receipt-vs-current distinction; WIP classes; staleness;
  response caps; NanoClaw `materializeTaskInputs` byte-count and SHA-256
  confirmation of an `available` claim against a real local HTTP stand-in.
- Not verified: a real Docker session container reading the mounted directory as
  its non-root user; a real forge read of branch/PR/commit state. Those remain
  operator-run or out of scope for this preflight.
