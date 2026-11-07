---
status: implemented
created: 2026-11-02
updated: 2026-11-06
---

# Task helpers (`argus-task`)

# 1. Purpose and Scope

Agents spend model turns on mechanical steps: sleeping and re-checking CI, reading a PR's
state across several calls to decide whether it can merge, scrolling raw CI logs to find
the failure, and the push / open-or-update-PR sequence. Each of those turns carries the
whole context, so they cost tokens and slow the attempt down, and while an agent sleeps it
neither notices a steer nor tells the operator what it is waiting for.

This spec adds **`argus-task`**, a deterministic worker helper that does those steps in
one call and returns a short, decided answer. Goals:

- **React to events** (CI finished, review posted, steer, cancel) within one check
  interval, without a model turn per check.
- **Shorten attempts** by replacing multi-turn mechanical sequences with one command.
- **Show waiting** on the dashboard, so a waiting task reads as waiting, not quiet.

In scope: the `argus-task` CLI and its four commands, a file channel between the helper
and the NanoClaw host (`.git/argus/` in the task workspace), the `waiting` progress
milestone, and the task prompt line that points agents at the helper.

Related: [Gitea helper](gitea-helper.spec.md), [GitHub
repositories](github-repositories.spec.md) (the two forge clients this builds on),
[progress milestones](progress-milestones.spec.md), [stall
visibility](stall-visibility.spec.md), [task steering](task-steering.spec.md),
[engines](engines.spec.md).

# 2. Non-Goals

- **No park-and-wake.** The attempt keeps its worker slot while it waits. Ending a turn
  with `WAIT: <condition>` and letting Argus wake a new turn on the event is a separate
  spec (4).
- **No REST or MCP surface.** `pr-ready` and `ci-digest` are worker-only here (4).
- **No merge, approve or conflict resolution.** `pr-ready` reports; `deliver` stops on a
  rebase conflict.
- **No waiting on tasks.** A claimed task's dependencies have all `succeeded`
  ([task dependencies](task-dependencies.spec.md) 3.1), so there is nothing to wait for.
- **No enforcement.** Agents are told to prefer the helper; nothing blocks the old way.
- **No new credentials, service, table or loop in Argus.** The helper uses the existing
  forge clients through the OneCLI proxy and never holds a token.
- **No format-specific CI parsers** in this version (3.5).

# 3. Decisions Made

## 3.1 Shape

One file, `integrations/nanoclaw/argus-task.mjs`, installed in agent images as
`/usr/local/bin/argus-task` beside `gitea-api.mjs` and `github-api.mjs`, which it imports.
It picks the forge client from the repository URL (`repositoryClient`), reads the same
`ARGUS_REPOSITORY_URL` / `--repository` and `ARGUS_REPOSITORIES_JSON` allowlist as the
forge helpers, and adds no HTTP client of its own. It is on `PATH` for the harness and for
every delegated executor.

Commands: `wait`, `pr-ready`, `ci-digest`, `deliver`, `attach`. `attach` talks to the Argus channel's files, not to the forge, and needs no repository (3.10). Every other command is stateless: it
derives everything from the forge, git and its arguments, so a call killed by an engine
timeout loses nothing and the next call carries on.

## 3.2 Output and exit codes

Every command prints exactly one JSON line on stdout; diagnostics go to stderr. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Condition met (`wait`), ready (`pr-ready`), done (`ci-digest`, `deliver`), stored with a receipt (`attach`) |
| 10 | Still pending: the wait cap was reached first (`attach`: no receipt yet) |
| 11 | Interrupted: a steer or cancel is pending (3.4) |
| 12 | Not ready (`pr-ready`), stopped on a conflict (`deliver`) or refused (`attach`); `reasons` or the error code says why |
| 1 | Error (forge unreachable, repository not allowlisted, git failure) |
| 2 | Usage error |

No credential appears in stdout, stderr or any file the helper writes.

## 3.3 `wait`

```
argus-task wait ci --pr N [--sha SHA]
argus-task wait review --pr N [--since CURSOR]
    [--interval SECONDS] [--max SECONDS]
```

- `ci` returns when no check on the head SHA (or `--sha`) is pending, including
  after one has already failed, so one result covers the whole run. The JSON carries the
  overall `state` and the failed checks' names; a pending result says how many failed so
  far. Each check's state is normalized and anything unrecognized, missing or malformed
  (a Gitea `warning`, a status without a state) is `unknown`, never success. The overall
  `state` is the first that applies: `failure` (failure, error, cancelled, timed_out),
  `pending` (pending, queued, in_progress, waiting, requested, running), `unknown`,
  `success` (success, neutral: executed, non-blocking), `skipped` (every check skipped;
  a mix of skipped and success is `success`), or `missing` (no checks). An `unknown`
  result exits 0 with state `unknown`: the wait is over, but the evidence is not a pass.
- `review` returns when a review or comment newer than `--since` appears. The JSON
  carries the new items' ids, authors and kinds (not their bodies; the agent reads those
  with the forge helper) and a `cursor` for the next call.
- `ci` ignores agentic-review contexts (they are review, not CI) and keeps the newest
  status per context. No status at all is "no checks reported yet", still pending.
- The helper checks the forge every `--interval` seconds (default 30, minimum 10) and
  returns after `--max` seconds at most. `--max` defaults to `ARGUS_TASK_WAIT_MAX_S`
  (110 when unset, never above 540) and is clamped to it (3.8). Exit 10 carries a
  one-line status (`"3 of 5 checks running"`) so a re-call costs one short turn.
- Between forge checks it reads the interrupt marker (3.4) once a second, a local file
  read, so a steer ends the wait within a second.
- One deadline, `start + --max`, bounds the whole wait. Every forge request in a check
  (the PR head lookup and each page of comments, reviews, statuses and check runs)
  re-reads the clock before it starts, is refused once the deadline has passed, and is
  cut to the time left (at most 10 s). Inside a wait the forge client does not retry (the
  next check is the retry), so no backoff can cross the deadline. A network error, 5xx or
  429 counts as still pending, not as a failure; a refused request ends the wait as
  pending (exit 10). A check that finishes at or after the deadline is not reported as
  done: the result is exit 10, never a late success.

## 3.4 The `.git/argus/` file channel

The helper cannot reach Argus: the agent token stays on the NanoClaw host. The host and
the helper talk through two files in `<task workspace>/.git/argus/` instead. Inside `.git`
nothing can be committed, so no exclude rule is needed; the helper finds the directory
with `git rev-parse --absolute-git-dir`, and outside a repository it runs without it.

- **`interrupt`** (host → helper). When the channel injects a steer into the session for
  the claimed attempt, it writes `{reason: "steer", at}`, never the steer text. A steer is
  read by the agent only after its current tool call returns, so `wait` exits 11 when the
  marker's `at` is later than the moment that wait started. The marker is never deleted:
  a later steer overwrites it, and an older one does not interrupt a new wait.
- **`waiting.json`** (helper → host). `wait` writes `{on: "ci"|"review", ref, since}` when
  it starts and removes it when it returns, except on exit 10, where it stays for the
  re-call. A re-call for the same `on` and `ref` keeps `since`, so the dashboard timer and
  the review cursor survive the cap. On each heartbeat the host posts a present record as
  a `waiting` milestone (3.7) when it changed, and again every 5 minutes while it lasts, so
  a long wait never reaches the 20-minute quiet flag. A file not modified for 15 minutes is
  stale and ignored.

The workspace belongs to the container, which can swap any directory in it for a symlink
at any moment, so the host treats both files as untrusted. It walks from its own group
directory to `.git/argus` one component at a time, opening each as a directory without
following a symlink, and then reaches the files only through the last open handle
(`/proc/self/fd/N/…`), so nothing is resolved by path again after a check. It writes the
marker through a new temporary file and a rename, opens `waiting.json` non-blocking so a
FIFO is refused instead of hanging the heartbeat, reads at most 4 KiB, and rebuilds the
milestone field by field (`ref` printable ASCII, `since` a past ISO time).

## 3.5 `ci-digest`

```
argus-task ci-digest --pr N | --run RUN_ID [--max-bytes 2048]
```

Finds the failed jobs and steps for the PR's head SHA (or one run), and from each
failed step's log keeps the lines that look like errors (`error`, `failed`, `FAIL`,
`Traceback`, `assert`, `panic`, exit-status lines) plus the last 20 lines. It strips ANSI
codes and timestamps, and removes repeated lines. `--max-bytes` (default 2048) bounds the
printed JSON line itself, field names and escaping included: excerpts are added until the
next one would not fit, and `truncated` says something was left out. The JSON names the job, step and run id of each
excerpt, so the agent can fetch the full log with the forge helper when the digest is not
enough. Parsers for specific formats (pytest, vitest, ruff, tsc, biome) are added only for
a format this pass handles badly, each with a fixture.

## 3.6 `pr-ready` and `deliver`

`argus-task pr-ready --pr N [--skip-review]` exits 0 or 12 with `reasons` from a closed
set:

| Reason | When |
| --- | --- |
| `ci_running` | A check on the head SHA has not finished |
| `ci_failed` | A check on the head SHA failed |
| `ci_missing` | No check has reported on the head SHA |
| `ci_unknown` | A check on the head SHA reports a state the helper does not recognize (and none is failing or running) |
| `head_moved` | The head changed since the PR read: it is read again after all evidence is loaded, also with `--skip-review`; a ready result names the head checked; call again |
| `review_missing` | No agentic review with a finding count covers the head SHA (`review-findings`) |
| `review_findings` | The agentic review for the head SHA has findings |
| `changes_requested` | A reviewer's latest non-comment review requests changes (dismissed ones are skipped) |
| `not_mergeable` | The forge reports `mergeable: false` |

`--skip-review` drops the review reasons for repositories without agentic review.
Unresolved review threads are not a reason: GitHub's REST API does not expose thread
resolution, and a blocking review already shows as `changes_requested`. It never merges
or approves.

`argus-task deliver [--title T] [--body-file F] [--base B]` pushes the current branch,
creates the PR or updates the open one for the branch, and prints `{url, number,
head_sha, created}`. If the branch is behind its base it rebases first, only when the
rebase applies cleanly; on a conflict it aborts the rebase it started, leaves the branch
as it was, and exits 12 with `reasons: ["rebase_conflict"]` and the conflicting paths. It
never force-pushes a branch it did not just rebase, and pushes with `--force-with-lease`
when it did.

`merge-base --is-ancestor` exiting 1 means the branch is behind; any other failure is a Git
error. Any other Git failure exits 1 with a bounded error (`git_failed`: subcommand, exit
status, the first bytes of stderr) and no push or PR change. A merge, rebase, cherry-pick or revert
already in progress is refused (`git_operation_in_progress`) and left untouched. A tracked
change in the worktree when a rebase is needed is refused (`dirty_worktree`, with the count
and first paths); nothing is stashed or reset. Only a rebase this invocation started is
aborted; if the abort fails, that is the error.

## 3.7 `waiting` milestone

[Progress milestones](progress-milestones.spec.md) gains one kind:

| `kind` | `data` |
| --- | --- |
| `waiting` | `on` (`ci` or `review`), `ref` (string, at most 200 characters), `since` (ISO time) |

The dashboard renders it as "Waiting on CI for #412 · 6 min" from `since`. The next
milestone or note with a kind replaces it, as for every other kind. A posted `waiting`
milestone renews the quiet clock like any progress post.

## 3.8 Engines

The helper is a shell command, so every engine reaches it the same way, with no
engine-specific code in the helper:

| Engine | Decision |
| --- | --- |
| Claude | Supported. The image's Claude managed settings set `ARGUS_TASK_WAIT_MAX_S=480`. Claude Code's Bash tool defaults to a 120 s timeout (600 s at most), so the prompt tells the agent to pass a longer timeout; a call cut short loses nothing (3.1). |
| Codex, Cursor, Copilot, OpenCode (custom, local) as executors | Supported at the helper's default of 110 s: `argus-delegate` removes the harness's `ARGUS_TASK_WAIT_MAX_S` for every executor but Claude, until each engine's shell timeout is confirmed (4). |
| Codex or local as harness | Supported at 110 s: managed settings are Claude's only. |

A shorter cap only means more re-calls, each one short turn.

## 3.9 Adoption

Workers have no skill file; their guidance is the task prompt. For a task that can write
to its repository, the repository section gains one line: prefer `argus-task` to sleeping
and re-polling the forge helper, the four commands, exit codes 10, 11 and 12, and the
shell timeout. Images built before this change have no `argus-task`, so the line says to
check `command -v argus-task` first.

`argus-task` has no completion subcommand (only `wait`, `pr-ready`, `ci-digest`, `deliver`, `attach`).
A worker completes by replying `COMPLETE:` as plain text followed by the JSON object; the
Argus channel submits it to `/tasks/{id}/complete` and strips a `<message to="argus">`
envelope if the reply is exactly that.

## 3.10 `attach`: store a report or delegate output as an artifact

`argus-task attach --file ABSOLUTE_PATH [--name NAME] [--role ROLE] [--max SECONDS]` stores
a worker's file as an artifact of the attempt it runs, with no orchestrator and no
credential in the container. Before this, a worker's report or an `argus-delegate --out`
file reached Argus only if the orchestrator read it from the worker, which it cannot.

**Transport.** The container's agent group directory (the directory of
`ARGUS_TASK_LOG_FILE`, `/workspace/agent`) is the file bridge the host channel already
tails. `attach` reads the status file `argus-task-attach-status.json` (the channel offers
capability `artifact-attach`, protocol 1, for the claimed task and attempt, with its size
limit), copies the file into `argus-task-attach-spool/<nonce>` while hashing the copy,
appends one request line to `argus-task-attach.jsonl`, and polls
`argus-task-attach-receipts.jsonl`. The channel
(`integrations/nanoclaw/argus-attach-relay.ts`, started at claim, stopped when the attempt
ends) opens only `spool/<nonce>` (never a path the worker names, no links, one hard link),
hashes what it read, uploads with the host's token through the lease-holder upload,
reads the artifact back and calls the server-side verify, and only then appends the
receipt. The bytes cross the bridge once, as a file; no log line, request or receipt
carries them.

**Receipt.** One JSON line, built by the channel from values it holds:
`{stored, task_id, attempt, artifact_id, name, role, bytes, sha256, duplicate, renamed,
request_id}`. `attach` prints it only after checking that task, attempt, request, size
and SHA-256 are those of the file it sent. A failure prints no stdout and an
`{"error": {"code", "message"}}` line on stderr. The channel keeps the recorded
`artifact_id`s for the attempt; completion lists exactly those in `result.artifacts` and
the worker never types an ID.

**Exit codes** are the table in 3.2: 0 stored; 10 no receipt before the wait (default 20 s,
`--max` clamped to `ARGUS_TASK_WAIT_MAX_S`; the rerun adopts the pending request); 12
refused for `symlink`, `not_regular`, `outside_roots`, `bridge_file`, `empty_file`,
`too_large`, `stale_attempt`, `wrong_task`, `no_active_attempt`, `attempt_ended`,
`attempt_completed` or `invalid_request`; 1 for `prerequisite_missing` (below),
`file_changed`, `upload_failed`, `storage_mismatch`, `invalid_receipt`; 2 usage (relative
path, unknown role).

**Idempotence and names.** The request id is a hash of task, attempt, sanitized name,
role and content hash, so the same file, name and role gives the existing receipt
(`duplicate: true`) without a second artifact. The same name with other bytes is stored
under the hashed-name fallback (`report-<first 8 hex of sha256>.md`, the same
`withHashSuffix` rule as completion) and the receipt says `renamed: true`; artifacts stay
immutable and nothing is overwritten. The default name is the file's base name; names go
through `artifactName` (letters, digits, `.`, `_`, `-`, at most 200 characters).

**Roles** are the API's: `report`, `delegate_report`, `policy_snapshot`, `qa_receipt`,
`other`; none by default. Examples: `--file /workspace/agent/report.md --role report`,
`--file /workspace/project/.argus-delegate/report.md --role delegate_report`.

**File safety.** An absolute path to a regular, non-empty file under the allowed roots
(`ARGUS_TASK_ATTACH_ROOTS`, else `ARGUS_DELEGATE_ROOTS`, else `/workspace`, so sibling
directories and delegate `--out` directories are included); the final component is not a
link, the real path is under a root and is not one of the bridge's files. The size limit is
25 MiB, below the API's `ARGUS_ARTIFACT_MAX_BYTES` because the channel holds the bytes to
upload; the status file carries it. The file is hashed while it is copied and re-checked
(size and mtime of the open handle) afterwards: a file that changed is `file_changed`, and
the channel hashes the spooled copy again. At most 50 requests per attempt.

**Ownership.** The channel serves a request only for the task and attempt it claimed and
only while that claim is current. Another task, another attempt, a late request after the
attempt ended or completed, a malformed field or a wrong protocol gets an error receipt
with its code and uploads nothing. A request whose `request_id` or `nonce` is not well
formed is ignored. Only IDs the channel itself recorded feed `result.artifacts`.

**Missing support.** The status file is absent when the channel predates this feature, no
attempt is active, or the bridge files are not writable. `attach` then exits 1 with
`prerequisite_missing` naming `artifact-attach` and the file it looked for, stores
nothing and says not to ask the orchestrator to read worker-local files. It never reports
success without a receipt.

**Guidance.** Every task prompt (read-only ones too) carries the order: attach each report
or delegate output with `argus-task attach` BEFORE replying `COMPLETE:`; the printed
receipt is the proof; completion then lists the recorded artifacts; no receipt means not
stored. `argus-task attach --help` carries the examples and the exit codes. NanoClaw's
`send_file` is unchanged: it posts a chat attachment to the user, is not a receipt, and
does not create an artifact.

# 4. Open Questions

- **Shell timeouts of Codex, Cursor and Copilot.** Not confirmed from this repository;
  each needs a check against its CLI before its default moves above 110 s.
- **Park-and-wake.** A `WAIT:` outcome that frees the worker slot and wakes a new turn on
  the event. It needs lease, scheduling and resume rules; same-session resume exists only
  for Claude ([quota resume](quota-resume.spec.md)).
- **REST and MCP.** `pr-ready` and `ci-digest` would help orchestrators. Exposing them
  means moving the logic behind a Python service function the helper also calls.
- **Measured savings.** No baseline is taken. After rollout, compare turns and tokens per
  attempt in the [cost view](cost-view.spec.md) for tasks that deliver a PR.

# 5. Constraints

- Single node: no new service, table, migration or background loop. Argus changes are the
  `waiting` kind and the host channel's two files.
- No credentials in the container. The helper uses the forge helpers' proxy path; it has
  no token parameter and no direct network fallback.
- Forge load: a waiting task makes at most one forge read per interval (120 per hour at
  the default), well under GitHub's 5,000 per hour per token.
- Tests are deterministic: a fake forge and an injected clock, no sleeps or real
  intervals; `deliver` runs against real temporary git repositories. Covered: each
  `wait` condition, a failure while other checks run, a 5xx without retry, the cap
  clamp, interrupt by a newer marker and not an older one, the `pr-ready` reasons
  including a moved head, digest deduplication and the printed-size bound, `deliver` on
  a clean rebase and on a conflict, the `waiting` kind validation, the host's symlink
  refusals at each component, FIFOs, stale records and repost timing. `attach`
  (`argus-task-attach.test.mjs`, the real CLI against the real bridge with a fake store
  that keeps the bytes): the receipt and its checks, same-bytes retry, the hashed-name
  fallback, the pending exit and adoption, the wait cap, a forged receipt, an ended attempt,
  each file refusal, usage errors, missing support, stale, cross-task, late and malformed
  requests, a spool symlink and bytes that differ from the request, and every example in
  `--help`. `tests/test_attach_packaging.py` pins the install, the image paths, the shared
  file names and roles, and the guidance wording.

# 6. Implementation Consequences

- `integrations/nanoclaw/argus-task.mjs` and `argus-task.test.mjs` (run by the root
  `pnpm test`). It ships through the tools layer (`Dockerfile.tools-layer`, staged by
  `install-gitea-api.sh stage-tools-ctx`), which provisioning applies to every agent
  image, and the Radar project layer; the CI tools-layer smoke checks the binary.
- `ProgressKind.WAITING` and its validation in `argus_core.tasks`; the OpenAPI client is
  regenerated; the dashboard renders it (`milestones.tsx`); MCP `report_progress` lists it.
- NanoClaw channel: `argus-task-signals.ts` (marker, record reader, repost timing), called
  from `deliverSteers` and the heartbeat in `argus.ts`; installed by `install.sh`.
- `attach` (3.10): `argus-attach-relay.ts` (installed by `install.sh`), `startAttachRelay`
  and `storeAttachment` in `argus.ts`, and `ATTACH_GUIDANCE` in the task prompt. No Argus
  API, schema, MCP tool or tool-description change.
- `claude-managed-settings.json` sets the Claude cap; `argus-delegate` drops it for
  other executors (`waitCapEnv`).
- The repository section of the task prompt (3.9).
- **Merged code is not a live rollout.** Running containers keep their image, and the
  host channel changes with the next `install.sh`. Rollout is an image rebuild and a
  channel install, a canary on one worker (`argus-task --help`, `pr-ready` and
  `ci-digest` against an open PR, a `wait ci` that a steer interrupts, the `waiting`
  milestone on the task card), then recreating the remaining workers. Roll back by
  redeploying the previous image.
