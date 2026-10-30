# Task inputs: hand bytes to a later attempt through the NanoClaw channel

# 1. Purpose and Scope

[PR #80](mcp-operations.spec.md) lets an orchestrator attach a file to a task and
mark it `worker_readable`. That proved the manager side only. In the incident
that exposed the gap, a recovered 62,300-byte patch was attached by a model that
transcribed Base64 fragments, and the real NanoClaw worker of the next attempt
had no tool that read the attachment and nothing materialized in its workspace.
The agent container never holds an Argus token
([NanoClaw README](../../integrations/nanoclaw/README.md)), so "readable by
workers" meant nothing to it.

This spec closes the loop end to end:

1. **Upload** from a program, not a model: a byte stream with a size and
   SHA-256 the server checks before it publishes anything.
2. **Discovery**: the shared attachments of a task are listed as its *inputs*,
   with authoritative size, hash, provenance and the receipts below.
3. **Materialization**: the NanoClaw *channel* (the host process, which holds
   the worker's credential) downloads each input into a task-local directory the
   session container already mounts, verifies size and SHA-256, and only then
   shows it to the model.
4. **Consumption**: the model reads files, not an API. Required inputs that
   cannot be materialized and verified block the task before any repository
   work.

The scope is `attach` (REST and MCP), a read-only inputs view and a receipt
endpoint in the API, `integrations/nanoclaw/argus-inputs.ts` with its calls in
`runTask`, the `task-inputs` capability, and one operator-side upload helper.

# 2. Non-Goals

- No new storage. Bytes stay in the attachment store of
  [MCP operations 3.2](mcp-operations.spec.md); there is no second copy and no
  new table.
- No new worker credential, token in the container, network fetch of arbitrary
  URLs, host-path parameter or filesystem read by the remote MCP.
- No widening of grants: only attachments an orchestrator shared with
  `worker_readable` are inputs. An attachment is never a worker result artifact
  and never appears in `result.artifacts`.
- No cross-node transfer or caching: the channel downloads from Argus every
  time an attempt starts on an instance whose copy is absent or unverified.
- No rollout. Merged API support does not make a worker capable (3.8).

# 3. Decisions Made

## 3.1 Programmatic upload with integrity

`POST /api/tasks/{id}/attachments` takes two optional query parameters,
`expected_size` and `expected_sha256` (64 lowercase hex characters). After the
body is staged, a size or hash that differs is `422 integrity_mismatch`: the
staged file is discarded, no row exists and nothing is advertised. A body that
ends early (the connection drops) already fails before commit, so an incomplete
upload is never a usable attachment. The existing limits stay:
`ARGUS_ARTIFACT_MAX_BYTES` (streamed, `413 too_large`),
`ARGUS_ATTACHMENT_MAX_PER_TASK`, name rules, `409 artifact_exists`.

MCP `attach_artifact` gains `expected_sha256` with the same check. It stays
suitable for small text. Anything a model would have to transcribe is uploaded
with the REST endpoint by `scripts/attach_artifact.py` (standard library only):
it streams a local file with `Content-Length`, sends the size and SHA-256 it
computed, then calls `POST /artifacts/{id}/verify` and compares the response with
its own values. It exits non-zero on any difference and prints only id, name,
size and hash. It reads the admin or orchestrator token from
`ARGUS_TOKEN`, never from an argument. A retry after an unknown outcome lists
the task's inputs first and treats an attachment with the same name, size and
hash as done (idempotent); a same name with other bytes (or another
`worker_readable`) exits `3` without uploading. On success it prints one JSON
line including the `attachment:<id>:<sha256>` reference of 3.6.

## 3.2 The inputs view

`GET /api/tasks/{id}/inputs` (task read rule: managers, or a worker with a
lease grant on the task) lists the task's **inputs**: attachments with
`worker_readable = true`, each as the artifact metadata (id, name, media type,
size, sha256, `attached_by`, `created_at`) plus `receipts`. Unshared attachments
are never listed to workers and never counted. Managers also get
`unshared_attachments`, a count, so an orchestrator can see that something it
attached is not shared.

An input is in one of these states, recorded as evidence and never inferred:

| State | Meaning | Evidence |
| --- | --- | --- |
| attached | the row exists with the size and SHA-256 Argus computed | the artifact |
| worker_authorized | `worker_readable` was set at attach time | the artifact |
| materialized | a channel wrote the file into a task-local directory | receipt `materialized` |
| hash_verified | that channel re-read the file and its size and SHA-256 equal the artifact's | receipt `verified` |
| consumed | the in-session verifier re-hashed the file and the model was told it passed | receipt `consumed` |

`receipts` holds, per attempt and per state, the latest receipt (attempt,
state, time, reporter). A failed materialization is a `failed` receipt with a
reason code from 3.6 and no detail.

## 3.3 Receipts

`POST /api/tasks/{id}/inputs/{artifact_id}/receipt` is a worker write
(attempt-fenced like [progress](worker-lifecycle.spec.md): `409 stale_attempt`,
`403 not_leased`). Body: `attempt`, `state` (`materialized`, `verified`,
`consumed`, `failed`), `sha256` and `size` (required for `verified` and
`consumed`, optional otherwise; the channel sends them with `materialized`
too), and `reason` (only for `failed`: `unavailable`, `invalid`, `limit` or
`corrupt`). The channel maps the block reasons of 3.6 onto these codes
(`env_input_unavailable` → `unavailable`, `env_input_limit` → `limit`,
`env_input_invalid` → `invalid`, or `corrupt` when bytes arrived that are not
the artifact's: longer than its size or another hash). The server checks that the
artifact belongs to this task and is shared (otherwise `404`), and that a given
`sha256` and `size` equal the stored values (otherwise `422
integrity_mismatch`: a receipt cannot claim a hash the artifact does not
have). An identical latest receipt is not written twice (idempotent retry).
Receipts are `task.input` events `{artifact_id, attempt, state, reason?}`; they
carry no name, hash or content. There is no table.

## 3.4 Materialization by the channel

Before the model starts and before the task workspace is prepared, `runTask`
calls `materializeTaskInputs` (also for tasks without `repository:write`).

- **Where:** `<agent group dir>/task-inputs/<task id>/`, seen in the session as
  `/workspace/agent/task-inputs/<task id>/`. The task id must be a canonical
  lowercase UUID. The directory is created `0700` and each file `0400`, owned by
  the user the channel runs as, which is also the user of the session
  container; nothing is root-owned and nothing is world-readable. The `task-inputs`
  root and the task directory must be real, private directories of that user
  (opened with `O_DIRECTORY|O_NOFOLLOW`); otherwise the step fails with
  `invalid`.
- **Containment:** the session container runs as the same user and can rename
  any directory below the group dir, so a checked path proves nothing a moment
  later. The channel therefore holds the group dir, the `task-inputs` root, the
  task directory and `.partial` open and performs every create, read, unlink,
  rename and mode change relative to those handles (`/proc/self/fd/<fd>/<name>`
  on Linux, after checking that the handle path names the opened directory;
  where it does not, the step is `invalid`). An ancestor swapped for a link after
  the checks redirects nothing: writes land in the directory that was checked.
  Before reporting success the channel checks that the task path still leads,
  without a link, to that same directory; if it does not, every input is
  `failed` and a required one blocks with `env_input_invalid`.
- **Names:** the server's artifact name rule (letters, digits, `.`, `_`, `-`,
  at most 200, no leading `.`, `_`, `-`) is re-checked on the host. A name with a
  separator, a dot-only name, a Windows device name or a duplicate after case
  folding is `invalid`. Files are opened with `O_NOFOLLOW|O_NONBLOCK` and must be
  regular, single-link files of the channel user. A planted entry at a final or
  partial name is cleared without being followed and the input is downloaded
  again: a symlink or hard link loses only its name in the task directory (its
  target is never opened or changed), a directory is renamed into `.partial`
  as `<name>.displaced.<uuid>`. A planted entry never blocks later attempts.
- **Authority:** the list comes from `GET /tasks/{id}/inputs` with the channel's
  own worker token. Each entry must repeat the task id and be shared; its id,
  size and hash are the only truth. The channel never trusts a name, path or hash
  from the task text, the prompt or the continuation note.
- **Transfer:** `GET /artifacts/{id}/content` streamed to
  `.partial/<artifact id>.part` with a bounded buffer (never the whole file in
  memory, never in a log line, event or prompt). An interrupted transfer resumes
  with `Range: bytes=<partial size>-` and a matching `Content-Range`; a server
  answer that does not honour the range restarts the file, and a `416` drops the
  partial. A partial that already holds `size` bytes is hashed without a request.
  At most 3 failed transfers per input, 0.5 s and then 1 s apart; a transfer
  idle for 60 s is a failed transfer. This budget is separate from the hash
  retry below and from the task's attempt.
- **Verify, then publish:** when the partial file has `size` bytes, its SHA-256
  is computed from disk. Equal to the artifact's: the file is moved to its final
  name (rename, same directory), set `0400`, and the manifest is rewritten
  atomically. Different: the partial is deleted and the input is retried once
  from byte 0; a second mismatch is `corrupt`. A file is never visible under its
  final name before it is verified.
- **Idempotence:** an attempt that finds the final file re-hashes it. A match is
  reused without a download; a mismatch is replaced from Argus. `manifest.json`
  records, per input, `artifact_id`, `name`, `size`, `sha256`, `state` and
  the attempt; it holds no content and no token. Orphan `.partial` files of a
  finished or other task are never read. `manifest.json` and `consumed.json` are
  read through the held directories, at most 1 MiB, never through a link or from
  a FIFO; anything else is treated as absent.
- **Limits:** per input `ARGUS_TASK_INPUT_MAX_BYTES` (default
  `ARGUS_ARTIFACT_MAX_BYTES`, 100 MB), per task `ARGUS_TASK_INPUT_MAX_TOTAL_BYTES`
  (default 256 MiB) and `ARGUS_TASK_INPUT_MAX_FILES` (default 20). Over a limit
  is `limit`. A response with more bytes than the artifact's size is `corrupt`
  and stops reading at `size + 1`.
- **Verifier:** the channel writes `verify-inputs.mjs` (about 60 lines, only
  `node:fs` and `node:crypto`) next to the manifest. `node verify-inputs.mjs`
  re-hashes every file against the manifest, prints `name size sha256 ok|FAIL` and
  exits non-zero on any failure; on success it writes `consumed.json`. The
  channel posts a `consumed` receipt for each entry in that file when the attempt
  ends. A model that never ran the verifier leaves the input at `hash_verified`.

## 3.5 What the model sees

The prompt gets a `Task inputs` block of trusted lines built from verified data
only: the container path, size and SHA-256 of each materialized input, the
verifier command, and the rules: read these files in place; do not re-download or
fetch them from anywhere else; run the verifier before applying anything from
them (for example `git apply --check` against the exact base commit); report a
mismatch with `ASSIST`. The block never contains file content, a name the
channel rejected, an Argus URL or a token. A task with no shared inputs has no
block.

## 3.6 Required inputs, retries and failure reasons

The orchestrator marks one input as required by setting the continuation WIP
reference to `attachment:<artifact id>:<sha256>` through `PATCH
/tasks/{id}/continuation`. Argus accepts it only when the artifact belongs to
that task, is shared, and has that SHA-256 (`422 input_not_shared`,
`input_not_found` or `integrity_mismatch`), and only when the task's
`required_capabilities` include `task-inputs` (`422
input_capability_required`), so that only a worker that can materialize it
claims the task. A reference that is not exactly `attachment:` plus a
lowercase artifact UUID, `:` and 64 lowercase hex characters is `422
invalid_input_ref`. Worker observations may not set an `attachment:` reference
nor replace one (`422 input_ref_forbidden`): only a manager names a required
input. The checkpoint reference form is unchanged.

A required input that cannot be materialized and verified blocks the attempt
with a typed environment reason **before the workspace is prepared and before
any model runs**, so nothing in the repository is edited:

| Reason | When |
| --- | --- |
| `env_input_unavailable` | the input is missing from the list, not shared any more, Argus is unreachable after the retries, or the content endpoint answers `403`/`404`/`5xx` |
| `env_input_invalid` | the name or path is unsafe, the `task-inputs` root or task directory is a link, not private or replaced during the step, the hash or size differs after the retry, or the reference's hash differs from the artifact's |
| `env_input_limit` | a size, total or file-count limit is exceeded |

They join `ENV_BLOCK_REASONS` ([failure classes 3.3](failure-classes.spec.md)),
are retried as environment failures, and the dashboard labels them. A failed
optional input (shared but not required) does not block: its manifest entry is
`failed`, a `failed` receipt is posted, and the prompt says so. The block `error`
text names the reason and the artifact id, never a path outside the task
directory, content or a token.

## 3.7 Isolation

- A worker reads only inputs of tasks it holds or held a lease grant on
  ([worker read scope](worker-read-scope.spec.md): completing or releasing the
  lease keeps the read grant); another task is `403 not_leased`, an unshared
  attachment `403 attachment_scope` (unchanged). Writes, receipts included, are
  fenced by the current lease: a released or expired holder gets `409`. The
  channel asks for nothing else.
- Cross-project: the lease grant is per task, so a worker of another project's
  task has no access. The channel writes only below the group dir of its own
  instance and never reads another task's directory.
- Another attempt or worker of the same task discovers the same inputs through
  the same list; it needs no host path, no affinity to the instance that
  attached or first materialized them, and no credential beyond its own.

## 3.8 Capability, upgrade and activation

The channel advertises the `task-inputs` capability in its registration and
`features: ["steer", "task-inputs"]` in its heartbeats. A worker without the
current channel does neither, so a task that requires it is not claimed by an
old instance and merged API support is never read as installed capability.
Activation is an operator step this spec does not perform:

1. Install the channel (`install.sh`) into the NanoClaw checkout and rebuild it
   (`pnpm run build`); the session image does not change, because the files are
   mounted from the group dir and the verifier is plain Node.
2. Recreate the worker instances: running hosts keep the old channel.
3. Add `task-inputs` to the pool's capabilities, so the scheduler can start
   workers for tasks that require it.
4. Check `GET /api/agents` shows `task-inputs` in `runtime.features`.

Until then the orchestrator must not rely on a shared attachment for a retry.

## Verification boundary

What the repository tests prove, and what they do not:

- API tests: upload with expected size and hash, mismatch and truncation,
  inputs view, receipts (every channel reason code, integrity of a
  `materialized` receipt), isolation, reads by a former leaseholder,
  required-reference validation.
- Channel tests (vitest, in the installed NanoClaw checkout): materialization
  with a real local HTTP server, resume, corruption, symlink and traversal,
  limits, idempotence, blocking before workspace preparation, prompt contents,
  planted links and directories, and an ancestor swapped for a link after the
  checks (a test hook) leaving every file outside the group dir unchanged.
- Installed-runtime test (`scripts/task-input-e2e.sh`): a real Argus API on a
  disposable database and the channel built by `provision.sh` in a disposable
  NanoClaw checkout; worker A claims and is released, worker B claims the next
  attempt in another group dir, materializes the same bytes, runs the verifier and
  `git apply --check` in a disposable repository. It uses the channel code and
  verifier that production installs, not a mock.
- Not proven there: a Docker session container reading the mounted directory as
  its non-root user. That needs a Docker host and is an operator-run step
  ([task-inputs smoke](../runbooks/2026-10-30.task-inputs-smoke.ref.md)); the result of that step is recorded
  separately and this spec does not claim it.

# 4. Open Questions

- Whether `consumed` should also be reachable by a model that cannot run Node.
  The agent image has Node today.
- Whether inputs should be pruned from the group dir when the task finishes;
  workspace cleanup ([workspace cleanup](workspace-cleanup.spec.md)) removes the
  whole instance directory, which covers it for now.

# 5. Constraints

- Single node, local filesystem, no new service or table
  ([single-node](single-node.spec.md)).
- No network call inside a write transaction; receipts and the list use the
  existing event and artifact tables.
- Content never reaches prompts, events, logs or error text; only ids, sizes,
  hashes and reason codes do.
- English only.

# 6. Implementation Consequences

- `apps/api`: `expected_size`/`expected_sha256` on `attach`, `inputs` list and
  receipt routes, WIP reference validation, `task.input` event, OpenAPI and the
  generated client; MCP `attach_artifact.expected_sha256` and a manager
  `list_task_inputs` tool.
- `packages/core`: three `env_input_*` reasons; dashboard labels.
- `integrations/nanoclaw`: `argus-inputs.ts` and tests, `runTask` wiring,
  `task-inputs` capability and feature, `install.sh` copies, `provision.sh check`
  runs the tests, README upgrade section.
- `scripts/attach_artifact.py` and `scripts/task-input-e2e.sh`.
- Docs: [MCP operations](mcp-operations.spec.md), [worker
  lifecycle](worker-lifecycle.spec.md), [task
  continuation](task-continuation.spec.md), [failure
  classes](failure-classes.spec.md), the orchestrator skill and the docs index.
