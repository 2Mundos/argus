---
status: partial
created: 2026-10-27
updated: 2026-11-21
---

# Task workspace: a stable per-task git workspace, checked before the model

# 1. Purpose and Scope

A task on a project with a configured repository used to get a checkout only when
it listed `repository:write`. Workers on other tasks saw an empty workspace. This
spec covers the workspace half of P2 in
[worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md). The NanoClaw
channel gives each such task one git workspace at a fixed path, keyed by task id,
and checks it in code before any model starts ([project code
availability](project-code-availability.spec.md) decides when the channel
prepares it). A broken workspace blocks the task with a typed environment reason,
as the channel preflight in [failure classes](failure-classes.spec.md) 3.3 already
does for the repository.

The scope is the NanoClaw channel (`integrations/nanoclaw/argus-workspace.ts` and
its call in `runTask`) and the new block reasons in Argus.

# 2. Non-Goals

- No fetch, clone or push on the host, and no credential on the host side. The
  host only runs `git init` and `git remote add`. The worker fetches and pushes
  inside the session container, through the OneCLI gateway.
- No repair. An existing workspace is never reset, cleaned, re-pointed or
  deleted. Whatever it holds is the task's work in progress.
- No canonical host checkout is mounted, and PR operations do not move to the
  orchestrator.
- No resume of an interrupted attempt (P4); see
  [task checkpoint](task-checkpoint.spec.md).
- No executor credential probe. `env_executor_auth` stays reserved (failure
  classes 3.3).
- No retention, no byte quota and no cleanup of workspaces.
- No schema change; the API change is the claim's `prior_workspaces` and the new
  block reasons (3.7).

# 3. Decisions Made

## 3.1 Location, key, branch and isolation

- Host path: `<agent group dir>/task-workspaces/<task id>`. The agent group
  dir is `groups/<folder>` under the NanoClaw working directory, which is the
  instance directory `<instances_root>/<instance id>`. The session container
  mounts the group dir at `/workspace/agent`, so the worker sees
  `/workspace/agent/task-workspaces/<task id>`.
- The key is the task id, not the attempt. Every attempt of a task on the same
  instance gets the same path. The id must be a canonical lowercase UUID (Argus
  task ids are uuid4); anything else is refused, so no id can name a path
  outside the root.
- Branch: `argus/task-<first 8 hex of the task id>`.
- Origin is the task's normalized repository URL (`task.repository_url`, set by
  the repository preflight). A URL with userinfo is refused before anything is
  written, so a credential never reaches `.git/config`. The preflight may change a
  GitHub task's URL to GitHub's verified letter-case spelling
  ([GitHub repositories](github-repositories.spec.md) 3.3); an existing origin is
  never rewritten to follow it.
- The workspaces root and every workspace are created with mode `0700`. The
  session container runs as the host's uid, so the worker can use them.
- Host git is hardened against a `.git` the worker controls: inherited `GIT_*`
  variables are dropped, `GIT_CEILING_DIRECTORIES` is the workspaces root,
  system config is off, hooks go to `/dev/null`, fsmonitor is off, `git init`
  uses an empty template, and origin is read with `git config --file` (no
  includes). Each workspace sets `core.hooksPath` to
  `/usr/local/lib/argus/git-hooks` (the image directory that contains
  `commit-msg`) so the agent image's commit-msg hook runs for worker commits; the host still passes `-c core.hooksPath=/dev/null` on its
  own git invocations so preparation never executes worker hooks.

## 3.2 Lifecycle

1. The channel claims the task, takes the leases and runs the repository
   preflight (failure classes 3.3).
2. When the task's project has a configured repository, the channel prepares the
   workspace after preflight. Tasks on `global` or projects without a repository
   skip this step.
3. First attempt: the channel builds the workspace in a hidden temporary
   directory (`.tmp-<task id>-*`) under the root. It runs `git init` with HEAD
   on the task branch (unborn, no commits) and adds `origin`, then renames the
   directory into place. A workspace is therefore either absent or complete.
   When a concurrent prepare of the same task wins the rename, the loser
   removes only its own temporary directory and validates the winner's.
4. Later attempts: the workspace exists. Validation (3.4) runs read-only first;
   on failure nothing changes. When validation passes, the channel may set
   `core.hooksPath` to `/usr/local/lib/argus/git-hooks` if it is missing or
   wrong (worker-facing config only; host git still disables hooks). A
   concurrent first prepare that loses the rename runs the same adoption step
   after validation so mixed-version races still pick up the hook path. The
   workspace is reported with `created: false`.
5. Then the continuation preflight and injection run as before. The prompt
   carries: `Workspace: <container path>, branch <branch>, remote origin <url>;
   run git fetch and work in this directory; do not create another checkout.`
   It adds that the directory may hold work from an earlier attempt, to keep.
6. On a failure the channel calls `POST /tasks/{id}/block` with the reason and
   the redacted error, finishes the task and releases the repository lease, as
   for the other preflight checks. No model session starts and no attempt is
   spent.

## 3.3 Limits

- `ARGUS_TASK_WORKSPACE_MAX` (default `200`; a value that is not a positive
  integer means the default) caps the task workspaces in one group dir. Only
  task directories (UUID names) count; temporary directories do not.
- The cap applies only to creating a new workspace. A task whose workspace
  already exists is never refused, and nothing is removed to make room.
- There is no byte quota.
- [Workspace cleanup](workspace-cleanup.spec.md) works on `instances_root`.
  Task workspaces live inside an instance directory, so they are in scope for
  that cleanup once the instance is `terminated`, under its WIP rules (3.5
  there). Nothing else removes them.

## 3.4 Validation and failure reasons

Validation reads only and never writes. A failed prepare leaves the workspace
tree unchanged (including `.git/config`). After validation succeeds on an
existing workspace, the channel may update `core.hooksPath` when needed (3.2
step 4). In order: the workspace is a real directory
(not a symlink) whose real path is directly under the real workspaces root;
`.git` is a real directory and `git rev-parse --absolute-git-dir` names it;
origin is set exactly once and equals the expected URL (for a github.com repository,
a plain credential-free `https://github.com/{owner}/{repo}` with an optional `.git` whose
owner and repository differ only in letter case also matches; another scheme, port or
host, a query, fragment, trailing slash or extra segment does not, and Gitea compares
exactly); HEAD is
`refs/heads/<branch>` (an unborn branch is fine). The workspaces root itself
must be a real directory that resolves to `<real group dir>/task-workspaces`.

When HEAD is not the task branch, one more read decides the reason: the commits of
`refs/heads/<branch>`, and of a detached HEAD, that no remote-tracking ref reaches
(`git rev-list <ref> --not --remotes`). Any such commit is work that exists only in this
workspace, so the prepare is refused with `env_workspace_unpreserved_commits`, naming the
branch and up to ten commit SHAs. Without any, the reason is `env_workspace_branch_mismatch`.
The workspace is never reset, switched, archived or deleted either way. When HEAD is the task
branch the workspace is reused as it is: the branch stays checked out and its unpublished
commits stay reachable.

Every reason is in `ENV_BLOCK_REASONS`: the task is **blocked**, never
**failed**, maps to the `environment` class and spends no attempt.

| Reason | When |
| --- | --- |
| `env_workspace_unconfigured` | The agent group dir is unknown, or the task has no repository URL |
| `env_workspace_permission` | Creating or reading the workspace fails with `EACCES`, `EPERM` or `EROFS` |
| `env_workspace_invalid` | Not a directory, a symlink, resolves outside the root, not a git repository, or a malformed task id |
| `env_workspace_remote_mismatch` | Origin missing, different or set more than once, or a URL with credentials. The error names both URLs (without credentials) and, for GitHub, that letter case alone may differ |
| `env_workspace_branch_mismatch` | HEAD is not the task branch (another branch, or detached) |
| `env_workspace_unpreserved_commits` | HEAD is not the task branch (another branch, or detached) and commits of the task branch or of a detached HEAD are on no remote-tracking ref. The error names the branch and the SHAs |
| `env_workspace_limit` | A new workspace would exceed `ARGUS_TASK_WORKSPACE_MAX` |
| `env_workspace_prior_unverified` | A retry would start empty but an earlier attempt on another instance is not proven to have left nothing (3.7) |
| `env_workspace_prior_unavailable` | Such an attempt checkpointed unpublished work that this node cannot restore (3.7) |

A missing repository lease keeps `env_repository_unavailable`; the workspace
step does not run then. An existing origin URL with userinfo is reported as
"origin carries credentials", not echoed.

## 3.5 Ownership on the task read

`GET /tasks/{id}` (`TaskRead`) carries `workspace`, derived from the task's newest
attempt row, or null when the task has no attempt:

- `owner_agent_id`: the worker that holds the current workspace, the `agent_id` of
  the newest attempt.
- `owner_node_id`: the node the owner last reported on heartbeat
  (`ARGUS_NODE_ID`, else its hostname), or null when it never reported one. The
  agent's `runtime` carries it, so the API knows where the workspace lives without
  reading the worker's filesystem.
- `owner_status`: whether the owner is still there, from its stored state and
  heartbeat: `live` (active or draining, heard from within the loss threshold),
  `stale` (recorded live but silent past it), `ended` (gone or lost), or `unknown`
  (no agent row).
- `checkpoint`: whether the attempt's work is recoverable, from `checkpoint_state`
  ([task finalization](task-finalization.spec.md) 3.2):

| `checkpoint_state` | `workspace.checkpoint` |
| --- | --- |
| `saved` | `available` |
| `failed`, `nothing_to_save` | `unavailable` |
| `running`, none recorded, unrecognised | `unknown` |

No path rule: the response never carries a host path, the `checkpoint:<id>:<sha>`
reference or `checkpoint_detail`; the API knows no workspace path. The node id is an
identifier, not a host path. The field sits behind the task read scope. Lists and the
MCP `get_task` tool (a pass-through) show the same single-task read only. The
dashboard's task detail "Workspace" row shows the owner, its node id when reported,
and its status.

## 3.6 The continuation's WIP claim comes from git

After the workspace is prepared, the channel probes the task branch (exists? which
commits are on no remote-tracking ref?) and reports the result with
`POST /tasks/{id}/observations` as the contract's `wip` when the contract already holds a `wip`
claim or the probe finds unpublished commits. A missing branch is stated as missing; a branch
with every commit published is stated as having no unpublished commits; otherwise the claim
names the commits. It replaces an earlier agent's prose. A `wip` that is a checkpoint or
required-input reference is not a branch claim and is left alone
([task continuation](task-continuation.spec.md) 3.3).

## 3.7 Retries claimed by another instance

Workspaces are per instance directory, so a retry claimed by another instance finds none.
The claim (`ClaimedTask.prior_workspaces`, from the same attempt rows as 3.5) lists every
earlier claim row of the task across all retry generations, oldest first, ordered by claim
row (`task_attempts.id`) and excluding the current claim — not by the `attempt` number,
which restarts at 1 after an operator retry (`retry_task`). Each entry carries `attempt`,
`owner_agent_id`, `owner_node_id`, `owner_status` and `checkpoint_state` (what the worker
reported: `running`, `saved`, `nothing_to_save`, `failed`, or null for nothing reported).
No path, no checkpoint reference.

For a task with `repository:write`, `runTask` checks this list after the checkpoint restore
and before `prepareTaskWorkspace`, whether the workspace is missing, restored or already
present (`fencePriorWorkspaces`, read-only): an older local checkout cannot contain work a
later claim on another instance left (A, B, A), and the fence applies to attempt 1 of a
retry generation whenever earlier generations exist. A genuinely first claim (empty
`prior_workspaces`) passes, and an older server that omits `prior_workspaces` still passes
on attempt 1. Ordering and the reconciliation cutoff follow claim-row sequence (the order of
`prior_workspaces`), not `attempt` numbers. Attempts owned by this agent only advance the
cutoff when their checkpoint step ended (`saved` or `nothing_to_save`): the checkpoint step
only runs once the fence passes and `prepareTaskWorkspace` succeeds, so a blocked claim
reports nothing and never advances the cutoff, while claim rows older than a reconciled one
are ignored (same-instance behaviour is unchanged). A claim that died, was blocked or was
released before its checkpoint ended (no step, `running`, `failed`) proves nothing and
fences nothing off. Each claim row after the cutoff by another agent must be proven empty,
and the only proof is that worker's own `nothing_to_save` report (a clean or fully
published workspace). Otherwise nothing is created and the task is blocked, spending no
attempt, with the attempt, owner, node, status and checkpoint step named:

- `saved`: `env_workspace_prior_unavailable`. Its checkpoint is not in this node's store
  ([task checkpoint](task-checkpoint.spec.md) refuses foreign-node checkpoints).
- `failed`, `running` (never finished, so stale), not reported, or a server that sends no
  list on attempt > 1 (passes only when a workspace already exists or was restored, the
  legacy reuse): `env_workspace_prior_unverified`. Unknown is never reported as absent.

Recovery is only through supported inputs: a verified checkpoint in this node's store
(restored before the fence, byte-checked by its manifest), or the work published from the
other instance. Nothing is copied, reset or deleted, and the fence never writes, so
concurrent attempts and stale leases cannot overwrite anything.

# 4. Open Questions

- Retention: when should a finished task's workspace go, given that cleanup
  only acts on terminated instances?
- Byte quota: is a count cap enough, or does a large repository need a size
  limit per workspace or per group?
- Executor probe: `env_executor_auth` still needs a check that can run before the
  session container exists.
- P4: answered for one node by [task checkpoint](task-checkpoint.spec.md); the
  cross-node case stays open there.

## Open items

- **Cross-instance recovery.** 3.7 refuses to start empty without proof, but bytes
  stay on the other instance: nothing transfers them, and there is no operator waiver
  for a block whose earlier worker died before reporting.
- **No archive or publish fallback.** A refused workspace waits for an operator:
  there is no push (no grant for new refs) and no archive at the recorded path.

# 5. Constraints

- Single node ([single-node](single-node.spec.md)): local filesystem only, no new
  service, no schema change.
- No network call and no credential in the host step.
- Never delete or rewrite existing workspace content.
- English only, no secrets in the repository or in `.git/config`.

# 6. Implementation Consequences

- `integrations/nanoclaw/argus-workspace.ts`: `taskWorkspacePath`,
  `taskWorkspaceContainerPath`, `taskBranch`, `maxWorkspacesFromEnv` and
  `prepareTaskWorkspace`, with `argus-workspace.test.ts` (real git, no network).
- `integrations/nanoclaw/argus.ts`: `runTask` runs the workspace step after the
  repository preflight and before the continuation preflight. The new
  `workspaceRoot` option defaults to `argusGroupDir`. The prompt gains the
  `Workspace:` line.
- `install.sh` copies the module and its test; `provision.sh check` runs the test.
- `packages/core/src/argus_core/tasks.py`: the six reasons join
  `ENV_BLOCK_REASONS`. The dashboard inbox has a label for each.
- [Failure classes](failure-classes.spec.md) 3.3 lists the workspace check.

## Verification boundary

- Commands: `uv run pytest -q -p no:cacheprovider apps/api/tests/test_project_repository.py`; `make test` also runs `integrations/nanoclaw/argus-workspace.test.ts` (real git, no network) and `argus-task-signals.test.ts`.
- Verified: unit and fixture level; the workspace preparation runs against real git on a local temporary repository.
- Not verified: remote forges and real clones over the network, a full worker image on a disposable node, real harness CLIs, and production.
