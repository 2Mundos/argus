---
status: implemented
created: 2026-11-02
updated: 2026-11-24
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
- **No approve, conflict resolution or merge outside a grant.** `pr-ready` and `wait` only
  report; `deliver` stops on a rebase conflict. Only `merge` (3.11) can merge, and only for
  an attempt the channel issued a merge authorization for.
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

Commands: `wait`, `pr-ready`, `ci-digest`, `deliver`, `merge`, `attach`. `attach` talks to the Argus channel's files, not to the forge, and needs no repository (3.10). Every other command is stateless: it
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
argus-task wait ci --pr N [--sha SHA] [--all]
argus-task wait review --pr N [--since CURSOR]
argus-task wait merge --pr N
    [--interval SECONDS] [--max SECONDS]
```

- `ci` watches one commit for the whole call: `--sha` as given, else the PR head read once
  at the first observation, never re-read to retarget. A re-call for the same `on` and
  `ref` without `--sha` reuses the sha stored in a pending `waiting.json` record (3.4) while
  it still is the PR head (a finished wait's record is never reused); if the head moved on, the new first observation is bound and the result
  says `rebound_from: <old sha>`. Every check reads the PR head afresh and returns
  `watched_sha`, `pr_head_sha` and `head_matches` (the head equals the watched sha; an
  abbreviated `--sha` matches by prefix). It fails fast: it returns as soon as one check on
  the watched commit has failed, with `state` `failure`, the checks failed so far, the number
  still `running`, the `diagnostics` below and an `action` (read the diagnostics, fix, push,
  wait again; it names `ci-digest --sha` for more and the checks still running, which the
  new push supersedes), so the worker does not idle while slow jobs finish a run that is
  already red. `--all` restores the whole-run
  wait: it returns only when no check is pending. Success always needs every check
  finished and none failed. Either way it returns only while `head_matches` is true. If the head is another commit the wait keeps
  going, bounded like any wait, with the status "PR head abc1234 is not the watched
  def5678"; it is never done on a commit the PR no longer points at, so a failure on an
  old head is never reported as an outcome. The JSON carries the
  overall `state` and the names of the failed checks of the watched commit only (not other
  commits' contexts); a pending result (`--all`) says how many failed so far. When the wait is done
  it also leaves the outcome (`state`, failed contexts) in the terminal record (3.4). Each check's state is normalized and anything unrecognized, missing or malformed
  (a Gitea `warning`, a status without a state) is `unknown`, never success. The overall
  `state` is the first that applies: `failure` (failure, error, cancelled, timed_out),
  `pending` (pending, queued, in_progress, waiting, requested, running), `unknown`,
  `success` (success, neutral: executed, non-blocking), `skipped` (every check skipped;
  a mix of skipped and success is `success`), or `missing` (no checks). An `unknown`
  result exits 0 with state `unknown`: the wait is over, but the evidence is not a pass.
- A done `ci` wait with state `failure` (fail-fast or `--all`) carries `diagnostics`: the
  `ci-digest` evidence (3.5) of exactly the watched commit within the default 2048 bytes,
  plus `checks`, the failed checks as `{context, state, link?}` (at most 20; `link` is the
  status's or check run's `target_url` under the 3.5 link rule). The fetch stops one request
  timeout (10 s, or half the time left when less remains) before the deadline. After it the
  PR head is read once more: if it moved, the failure belongs to an old head and is no
  outcome; the wait goes on with the "is not the watched" status. Unreadable logs, run or
  job lists, or a fetch out of time keep the failure and say so in the diagnostics (3.5).
  No other state (`success`, `skipped`, `unknown`, `missing`, pending) fetches or carries
  diagnostics. They stay in the command output: the terminal record (3.4) is unchanged.
- `review` returns when a review or comment newer than `--since` appears. The JSON
  carries the new items' ids, authors and kinds (not their bodies; the agent reads those
  with the forge helper) and a `cursor` for the next call.
- `ci` includes agentic-review contexts as CI checks, independent of review-comment policy, and keeps the newest
  status per context. No status at all is "no checks reported yet", still pending.
- A merged or closed PR ends a `ci` wait at once, with `status` `merged` or
  `closed without merge`, `merged` and, when present, `merged_at` and `merge_commit_sha`: no
  further check can ever finish on that PR, so spinning to the cap would leave the worker
  stale exactly when the owner merged. A `wait ci` therefore also reacts to the merge. That
  end carries no CI state, so it writes no terminal record (3.4); the pending one is cleared.
- `wait merge` only observes; it never merges (the authorized step is `argus-task merge`, 3.11). It waits for the PR to become terminal: `merged` (exit 0, `status` `merged`) or
  `closed without merge` (exit 0, `merged: false` — terminal, not success). It carries the
  head it read, and takes no `--sha`: it follows the PR, not a commit.
- The first checks are fast — 3, 6, 12 and 24 s — and then settle on `--interval` (default
  30, minimum 10), which is the ceiling, not a floor. A merge or a finished CI is noticed in
  seconds; a long wait still costs the interval rate. It returns after `--max` seconds at
  most. `--max` defaults to `ARGUS_TASK_WAIT_MAX_S`
  (90 when unset, never above 540) and is clamped to it (3.8). Exit 10 carries a
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
- **`waiting.json`** (helper → host). One record, pending or terminal.
  - *Pending.* `wait` writes `{on: "ci"|"review", ref, since}` when it starts, adding `sha`
    (the watched commit, 7-64 hex) once a `ci` wait has one. On exit 10 (the cap) it stays
    for the re-call. A re-call for the same `on` and `ref` keeps `since` and `sha` (3.3), so
    the dashboard timer and the review cursor survive the cap. The host posts it as a
    `waiting` milestone (3.7) when it changed, and again every 5 minutes while it lasts, so
    a long wait never reaches the 20-minute quiet flag.
  - *Terminal.* When a `ci` wait is done, the helper replaces the record with
    `{on, ref, since, sha, outcome: {state, failed}}`: `state` is the overall CI state
    (`failure`, `success`, `skipped` or `unknown`) and `failed` the failed contexts of the
    watched commit (at most 20, each printable ASCII of at most 100 characters; the helper
    replaces other characters with `?` and truncates). The host posts it once as the
    outcome milestone (3.7) and not again on later heartbeats. A record with an `outcome`
    is never continued: a new wait starts a fresh `since` and binds the head anew. A
    `review` wait that is done, a steer (exit 11) and an error remove the file: they have
    no outcome milestone (the API has no kind for them), and a steered agent acts at once
    and its next milestone replaces the `waiting` one.
  - The helper never reads or writes through a symlink there, and the file is best effort:
    a failed write is skipped, the wait carries on and returns the same result, only the
    milestone is lost. A file not modified for 15 minutes is stale and ignored, and so is one
    last modified before the current attempt was claimed: a record a previous attempt left
    in the same workspace is never relayed as this attempt's progress. The host reads only
    the claimed task's workspace and resets its repost state when a new attempt starts.

The workspace belongs to the container, which can swap any directory in it for a symlink
at any moment, so the host treats both files as untrusted. It walks from its own group
directory to `.git/argus` one component at a time, opening each as a directory without
following a symlink, and then reaches the files only through the last open handle
(`/proc/self/fd/N/…`), so nothing is resolved by path again after a check. It writes the
marker through a new temporary file and a rename, opens `waiting.json` non-blocking so a
FIFO is refused instead of hanging the heartbeat, reads at most 4 KiB, and rebuilds the
milestone field by field (`ref` printable ASCII, `since` a past ISO time; an `outcome` only
with a `sha` on a `ci` record, a known `state`, and bounded printable-ASCII `failed`
contexts, else the whole record is refused).

## 3.5 `ci-digest`

```
argus-task ci-digest --pr N | --sha SHA | --run-id RUN_ID [--max-bytes 2048]
```

Finds the failed jobs and steps for one commit (the PR's current head, or a full `--sha`)
or one run, and from each failed job's log keeps the lines that look like errors (`error`,
`failed`, `FAIL`, `Traceback`, `assert`, `panic`, exit-status lines) plus the last 20 lines.
Only runs whose `head_sha` equals the commit (case-insensitive) count, so a run of another
head is never attributed to it. `--max-bytes` (default 2048) bounds the
printed JSON line itself, field names and escaping included: excerpts are added until the
next one would not fit, and `truncated` says something was left out. Each excerpt names
its `run_id`, `job_id`, `job` and failed `step`, so the agent can fetch the full log with
the forge helper when the digest is not enough, and a `link` to the job (else the run)
only when it is an https URL without credentials on the forge's own host, at most 300
characters.

Forge output is untrusted. Every log, job, step and context name is sanitized before it is
printed: ANSI and other control sequences and timestamps are stripped, names are one line,
and secret-looking values (URL credentials, GitHub and AWS tokens, `Authorization` headers,
`Bearer` tokens, `password=`/`token=`/`secret=`/`api_key=` values) become `[REDACTED]`.
A forge error never fails the digest: a log that cannot be read is an excerpt with
`note: "log not read: <code>"`, and a run or job list that cannot be read sets
`diagnostics_unavailable: <code>`; the result still exits 0. Parsers for specific formats (pytest, vitest, ruff, tsc, biome) are added only for
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

The review reasons follow the trusted policy in `ARGUS_FORGE_REVIEW` (`required` |
`off`), set by the Argus channel from the claim ([settings](settings.spec.md) 3.12) and
printed as `review_policy` (`required`, `off`, or `unset`):

- `required`, or any other value: `review_missing` and `review_findings` as in the
  table. `--skip-review` is ignored.
- `off`: `review_missing` is never reported; an agentic review that covers the head
  with findings is still `review_findings`. The CI, `changes_requested`,
  `not_mergeable` and `head_moved` reasons are unchanged. `--skip-review` is ignored.
- `unset`, a helper outside an Argus session: `--skip-review` drops both review
  reasons for repositories without agentic review, as before.

Unresolved review threads are not a reason: GitHub's REST API does not expose thread
resolution, and a blocking review already shows as `changes_requested`. It never merges
or approves.

`argus-task deliver [--title T] [--body-file F] [--base B] [--undo-note "<reason>"]` pushes the current branch,
creates the PR or updates the open one for the branch, and prints `{url, number,
head_sha, created}`. If the branch is behind its base it rebases first, only when the
rebase applies cleanly; on a conflict it aborts the rebase it started, leaves the branch
as it was, and exits 12 with `reasons: ["rebase_conflict"]` and the conflicting paths. It
never force-pushes a branch it did not just rebase, and pushes with `--force-with-lease`
when it did.

`--undo-note` (one line, at most 200 characters) is for pushes nobody reviewed first: a
PR repair or a reviewer fix ([PR steward](pr-steward.spec.md) 3.4). When the push moves
the head of an existing open PR, `deliver` then posts one PR comment with the reason,
the old and new head, and the command that restores the old head
(`git revert --no-edit <old>..<new>` after a fast-forward, or a
`git push --force-with-lease=<branch>:<new> origin <old>:refs/heads/<branch>` after a
rebase). It posts nothing when the head did not move or no open PR existed. A failed
comment shows as `undo_note: "failed"` in the output (`"posted"` otherwise) and never
fails the delivery.

Before any remote update, after any rebase, `deliver` runs the checks `.githooks/pre-push`
runs for a developer push, on the exact commit it pushes: `make check-changed`, then
`gitleaks version` and `make secret-scan`. Worker checkouts do not enable that hook (their
`core.hooksPath` is the Argus commit-attribution hook), so the gate is in `deliver`. It needs
a clean worktree with no untracked files at HEAD, and it fails closed: a failing check exits
12 with `reasons: ["pre_push_failed"]`, the check name, its exit status and the last bytes of
its output, and nothing is pushed. When the failed check's output matches a missing-dependency
signature, the failure's `action` names the sync to run from the workspace root, then `deliver`
again: `uv sync --frozen --all-packages --group dev` for `No module named 'prometheus_client'` or
`ModuleNotFoundError: No module named 'argus_…'`; `pnpm install --frozen-lockfile` for `biome:`,
`tsc:` or `openapi-typescript: not found` and `ERR_PNPM_NO_OFFLINE_TARBALL`; both for `Network
connectivity is disabled`. The first signature in that order wins. `deliver` never runs the sync
and never uses `--offline`; other failures keep the generic action. A missing `make` or `gitleaks` exits 1
(`pre_push_unavailable`); HEAD or the worktree changing during the checks is refused
(`dirty_worktree`). Unlike the hook it never skips the secret scan. The checks run with
`GIT_*` variables removed, and `make hooks` and `--no-verify` are not used.

`merge-base --is-ancestor` exiting 1 means the branch is behind; any other failure is a Git
error. Any other Git failure exits 1 with a bounded error (`git_failed`: subcommand, exit
status, the first bytes of stderr) and no push or PR change. A merge, rebase, cherry-pick or revert
already in progress is refused (`git_operation_in_progress`) and left untouched. A tracked
change in the worktree when a rebase is needed is refused (`dirty_worktree`, with the count
and first paths); nothing is stashed or reset. Only a rebase this invocation started is
aborted; if the abort fails, that is the error.

## 3.7 `waiting` milestone and the outcome that replaces it

[Progress milestones](progress-milestones.spec.md) already has the kinds used; no kind is added.

| Record | `kind` | `data` | Message |
| --- | --- | --- | --- |
| pending | `waiting` | `on`, `ref`, `since` | "Waiting on CI for #412 at abc1234" (a review wait has no commit) |
| `failure` | `ci_failed` | `sha` | "CI failed at abc1234: python, secret-scan" (the failures seen so far; see below) |
| `success` | `ci_passed` | `sha` | "CI passed at abc1234" |
| `skipped` | `ci_passed` | `sha` | "CI skipped at abc1234" (skipped is not a blocker, as in `pr-ready`) |
| `unknown` | `ci_failed` | `sha` | "CI inconclusive at abc1234: unknown" (not a pass) |

A fail-fast wait posts `ci_failed` at the first failure with the failures seen so far. A
later wait on the same commit that sees more failed contexts updates it once; a failed set
that does not grow posts nothing. `pr-ready` (3.6) does not depend on this: a PR with a
failed check is not ready whatever is still running.

The API refuses data keys outside the closed set, so the pending post carries no `sha` (the
message names it) and the failed contexts travel only in the message of `ci_failed`. The
outcome milestone replaces the `waiting` one, so a finished wait never keeps saying
"Waiting on CI"; it is posted once per outcome, not per heartbeat. The dashboard renders a
pending one as "Waiting on CI for #412 · 6 min" from `since`. The next milestone or note with
a kind replaces any of them. A posted milestone renews the quiet clock like any progress
post. The delegate's milestone relay file ([progress milestones](progress-milestones.spec.md)
3.3) never carries `waiting`.

## 3.8 Engines

The helper is a shell command, so every engine reaches it the same way, with no
engine-specific code in the helper:

| Engine | Decision |
| --- | --- |
| Claude | Supported. The image's Claude managed settings set `ARGUS_TASK_WAIT_MAX_S=480`. Claude Code's Bash tool defaults to a 120 s timeout (600 s at most), so the prompt tells the agent to pass a longer timeout; a call cut short loses nothing (3.1). |
| Codex, Cursor, Copilot, OpenCode (custom, local) as executors | Supported at the helper's default of 90 s: `argus-delegate` removes the harness's `ARGUS_TASK_WAIT_MAX_S` for every executor but Claude, until each engine's shell timeout is confirmed (4). |
| Codex or local as harness | Supported at 90 s: managed settings are Claude's only. |

A shorter cap only means more re-calls, each one short turn.

## 3.9 Adoption

Workers have no skill file; their guidance is the task prompt. For a task that can write
to its repository, the repository section gains one line: prefer `argus-task` to sleeping
and re-polling the forge helper, the four commands, exit codes 10, 11 and 12, and the
shell timeout. Images built before this change have no `argus-task`, so the line says to
check `command -v argus-task` first. The delivery-end sentence depends on the grant: a task
the channel may authorize to merge (`taskMayMerge`) is told to finish with `argus-task merge
--pr N` (exit 10 means run it again) and to complete only once the forge shows the PR merged;
any other task is told `wait merge` only observes and never merges.

`argus-task` has no completion subcommand (only `wait`, `pr-ready`, `ci-digest`, `deliver`, `merge`, `attach`).
A worker completes by sending `COMPLETE:` followed by the JSON object with the send_message tool to
the `argus` destination; the Argus channel submits it to `/tasks/{id}/complete`. On a task run the
runner delivers only explicit tool sends, so a `COMPLETE:` written as plain reply text stays in the
run log and never reaches Argus, and a `<message to="argus">` block in the final text is dropped
(`dispatchResultText`, the `routing.taskRun` branch). An unwrapped final reply gets a nudge that
names `send_message` and quotes the reply (`patch-task-unwrapped-nudge.py`), so a worker that
replied in plain text can still send the completion in the same attempt.

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

## 3.11 `merge`: the authorized delivery end

`wait merge` only observes (3.3); PR 428 showed a worker that ran `deliver` and `wait merge` and
then waited for a merge nobody performed. `argus-task merge --pr N [--sha S] [--merge-method
merge|squash|rebase] [--interval S] [--max S]` is the one step that merges, in one bounded call.

**Authorization.** Before any forge request it verifies the channel's per-attempt merge
authorization (`ARGUS_MERGE_AUTH_KEY`, `_TASK`, `_ATTEMPT` and the HMAC `ARGUS_MERGE_AUTHORIZED`,
[GitHub repositories](github-repositories.spec.md) 3.4), the check `argus-github-api merge`
makes. The channel mints it only for a task that may merge (`taskMayMerge`): `auto_merge` with
deliverable `pr_merged`, or an orchestrator-authored `delivery.allow_merge`, plus
`repository:write`. A missing, bare, forged, stale or other-attempt token ends the call with an
error and no request; a `pr_open` or `pr_green` task, a read-only task and a worker-authored
input never get one. A flag or an unverifiable variable is not a grant. The prompt names `merge`
only for such a task (3.9); every other task is told that `wait merge` never merges.

**Gates, re-evaluated on every poll against the current head** (first checks 3 s apart, settling
on `--interval`, cap as in 3.3):

| Observation | Result |
| --- | --- |
| Merged | exit 0, `already_merged`; never a second PUT. A merged PR without a merge commit is an error |
| Closed without merge | exit 12, `blocked: ["pr_closed"]` |
| `--sha` is not the head | exit 12, `head_moved` |
| Failed check | exit 12 at once, `ci_failed` with `diagnostics` for the head (3.3), `action` points at `ci-digest --sha` |
| Every check skipped | exit 12, `ci_not_executed` (skipped is not execution) |
| Running, no checks yet, unknown state, forge error, rate limit | waiting; at the cap exit 10, `waiting` names why |
| Review policy `required` (or unset or unknown) and no zero-finding agentic review of the exact head | waiting `review_missing` |
| Agentic review findings on the head, even with policy `off` | exit 12, `review_findings` |
| Standing changes-requested review | exit 12, `changes_requested` |
| `mergeable_state` `blocked`, `behind`, `dirty`, `draft`, or `mergeable: false` | exit 12, `merge_<state>` or `not_mergeable` |
| `mergeable_state` not `clean`/`has_hooks` (unknown, null) | waiting `mergeability_unknown` |

The forge review policy is `ARGUS_FORGE_REVIEW` (settings 3.12). The independent task review
(`review.independent`, run by Argus after `COMPLETE`), manual task acceptance and the forge
agentic review are different gates: `merge` neither waits for nor substitutes the first two, and
a disabled task review never relaxes branch protection, required reviews or checks, all of which
GitHub enforces on the PUT too.

**Contradictory check runs.** GitHub can report a check run as `in_progress` while it carries a
`conclusion` and `completed_at` (PR 428). The client marks such a run `contradictory` and keeps it
pending; `merge` accepts it only when its own workflow run (from `details_url`), read from the
forge, is `completed`/`success` for this exact head. Otherwise the state is unknown (`ci_contradictory`):
waiting, never success. Fixture: `argus-task-merge.test.mjs`.

**Failure diagnostics.** A `ci_failed` head gets the same `diagnostics` and head re-read as a
failed `wait ci` (3.3). Every gate reads the current head's statuses only, so a failure on an
old head never blocks or is reported for a newer one; if the head moved during the fetch, the
new head is gated at the next poll instead.

**Merge.** When every gate holds, it re-reads the PR, confirms the head is unchanged, checks the
steer marker and the budget, then calls `mergePullRequest` (PUT `/pulls/N/merge` with the head
SHA, 30 s grace) and reads the PR back: merged, with a merge commit. The output is `{merged: true,
merged_this_call, pr_head_sha, merge_commit_sha, merged_at}`. A head that moves before the PUT is
re-gated, not merged. If the forge refuses the PUT, the PR is read again: merged means success,
otherwise exit 12 `merge_refused` with the error and no retry. A transient error during the PUT
is retried by the next poll, which sees the PR merged if the first request landed, so a re-run
after a cap, a steer or an ambiguous response never merges twice. A steer ends the call with exit
11 before any further request; the `waiting.json` record is `on: merge` while pending.

# 4. Open Questions

- **Shell timeouts of Codex, Cursor and Copilot.** Not confirmed from this repository;
  each needs a check against its CLI before its default moves above 90 s.
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
  including a moved head, digest deduplication and the printed-size bound, failure
  diagnostics (a pending check that fails with its sanitized excerpt and redacted tokens,
  several failures within the budget, a head that moves during the fetch, runs of other
  heads ignored, unreadable logs and run lists, a fetch out of time, none for success or
  unknown), `merge` gating only the current head (an old head's failure with a green new
  head merges that head; a green head replaced by a failed or pending one is refused),
  `ci-digest --sha`, the host ignoring an earlier attempt's or another task's record, `deliver` on
  a clean rebase and on a conflict, the pre-push gate (order, the rebased commit, a failing check or scanner, a dirty tree; no push on failure), the `waiting` kind validation, the host's symlink
  refusals at each component, FIFOs, stale records and repost timing. `attach`
  (`argus-task-attach.test.mjs`, the real CLI against the real bridge with a fake store
  that keeps the bytes): the receipt and its checks, same-bytes retry, the hashed-name
  fallback, the pending exit and adoption, the wait cap, a forged receipt, an ended attempt,
  each file refusal, usage errors, missing support, stale, cross-task, late and malformed
  requests, a spool symlink and bytes that differ from the request, and every example in
  `--help`. `tests/test_attach_packaging.py` pins the install, the image paths, the shared
  file names and roles, and the guidance wording.

# 6. Implementation Consequences

- `integrations/nanoclaw/argus-task.mjs`, `argus-task.test.mjs` and `argus-task-merge.test.mjs` (run by the root
  `pnpm test`); `github-api.mjs` flags contradictory check runs and `gitea-api.mjs` exports the
  merge-authorization check for `merge` (3.11). It ships through the tools layer (`Dockerfile.tools-layer`, staged by
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
  channel install, a canary on one worker (`argus-task --help` lists `merge`, `pr-ready` and
  `ci-digest` against an open PR, a `wait ci` that a steer interrupts, the `waiting`
  milestone on the task card), then recreating the remaining workers. Roll back by
  redeploying the previous image.

## Verification boundary

- Commands: `make test` (runs `integrations/nanoclaw/argus-task.test.mjs`, `argus-task-merge.test.mjs`, `argus-task-attach.test.mjs` and `argus-task-signals.test.ts`); `uv run pytest -q -p no:cacheprovider integrations/nanoclaw/tests/test_attach_packaging.py`.
- Verified: unit and fixture level with a fake forge, an injected clock and a fake store; the attach CLI runs against the real bridge with a fake store.
- Not verified: a real forge or CI provider, the helpers inside a deployed worker image, real harness CLIs, and production.
