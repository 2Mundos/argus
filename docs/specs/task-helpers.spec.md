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

Commands: `wait`, `pr-ready`, `ci-digest`, `deliver`. Every command is stateless: it
derives everything from the forge, git and its arguments, so a call killed by an engine
timeout loses nothing and the next call carries on.

## 3.2 Output and exit codes

Every command prints exactly one JSON line on stdout; diagnostics go to stderr. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Condition met (`wait`), ready (`pr-ready`), done (`ci-digest`, `deliver`) |
| 10 | Still pending: the wait cap was reached first |
| 11 | Interrupted: a steer or cancel is pending (3.4) |
| 12 | Not ready (`pr-ready`) or stopped on a conflict (`deliver`); `reasons` says why |
| 1 | Error (forge unreachable, repository not allowlisted, git failure) |
| 2 | Usage error |

No credential appears in stdout, stderr or any file the helper writes.

## 3.3 `wait`

```
argus-task wait ci --pr N [--sha SHA]
argus-task wait review --pr N [--since CURSOR]
    [--interval SECONDS] [--max SECONDS]
```

- `ci` returns when every check on the head SHA (or `--sha`) has finished, including
  after one has already failed, so one result covers the whole run. The JSON carries the
  overall result and the failed checks' names; a pending result says how many failed so
  far.
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
- A check never outlives the cap: inside a wait the forge client does not retry (the
  next check is the retry), each request is cut to a third of the time left (at most
  10 s), and a network error, 5xx or 429 counts as still pending, not as a failure.

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
| `head_moved` | The head changed between the PR read and the review read; call again |
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
rebase applies cleanly; on a conflict it aborts the rebase, leaves the branch as it was,
and exits 12 with `reasons: ["rebase_conflict"]` and the conflicting paths. It never
force-pushes a branch it did not just rebase, and pushes with `--force-with-lease` when it
did.

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
  refusals at each component, FIFOs, stale records and repost timing.

# 6. Implementation Consequences

- `integrations/nanoclaw/argus-task.mjs` and `argus-task.test.mjs` (run by the root
  `pnpm test`). It ships through the tools layer (`Dockerfile.tools-layer`, staged by
  `install-gitea-api.sh stage-tools-ctx`), which provisioning applies to every agent
  image, and the Radar project layer; the CI tools-layer smoke checks the binary.
- `ProgressKind.WAITING` and its validation in `argus_core.tasks`; the OpenAPI client is
  regenerated; the dashboard renders it (`milestones.tsx`); MCP `report_progress` lists it.
- NanoClaw channel: `argus-task-signals.ts` (marker, record reader, repost timing), called
  from `deliverSteers` and the heartbeat in `argus.ts`; installed by `install.sh`.
- `claude-managed-settings.json` sets the Claude cap; `argus-delegate` drops it for
  other executors (`waitCapEnv`).
- The repository section of the task prompt (3.9).
- **Merged code is not a live rollout.** Running containers keep their image, and the
  host channel changes with the next `install.sh`. Rollout is an image rebuild and a
  channel install, a canary on one worker (`argus-task --help`, `pr-ready` and
  `ci-digest` against an open PR, a `wait ci` that a steer interrupts, the `waiting`
  milestone on the task card), then recreating the remaining workers. Roll back by
  redeploying the previous image.
