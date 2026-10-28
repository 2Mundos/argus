# Task workspace: a stable per-task git workspace, checked before the model

# 1. Purpose and Scope

A `repository:write` task used to start its model session with no checkout. The
worker made one wherever it liked, and a later attempt of the same task could not
find it. This spec covers the workspace half of P2 in
[worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md). The NanoClaw
channel gives each such task one git workspace at a fixed path, keyed by task id,
and checks it in code before any model starts. A broken workspace blocks the task
with a typed environment reason, as the channel preflight in
[failure classes](failure-classes.spec.md) 3.3 already does for the repository.

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
- No resume of an interrupted attempt (P4). The workspace makes a resume
  possible; how the worker uses it is not specified here.
- No executor credential probe. `env_executor_auth` stays reserved (failure
  classes 3.3).
- No retention, no byte quota and no cleanup of workspaces.
- No schema change and no Argus API change beyond the new block reasons.

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
  written, so a credential never reaches `.git/config`.
- The workspaces root and every workspace are created with mode `0700`. The
  session container runs as the host's uid, so the worker can use them.
- Host git is hardened against a `.git` the worker controls: inherited `GIT_*`
  variables are dropped, `GIT_CEILING_DIRECTORIES` is the workspaces root,
  system config is off, hooks go to `/dev/null`, fsmonitor is off, `git init`
  uses an empty template, and origin is read with `git config --file` (no
  includes).

## 3.2 Lifecycle

1. The channel claims the task, takes the leases and runs the repository
   preflight (failure classes 3.3).
2. If the task needs `repository:write`, the channel prepares the workspace.
   Tasks without it are untouched: the workspaces root is not even resolved.
3. First attempt: the channel builds the workspace in a hidden temporary
   directory (`.tmp-<task id>-*`) under the root. It runs `git init` with HEAD
   on the task branch (unborn, no commits) and adds `origin`, then renames the
   directory into place. A workspace is therefore either absent or complete.
   When a concurrent prepare of the same task wins the rename, the loser
   removes only its own temporary directory and validates the winner's.
4. Later attempts: the workspace exists and is validated read-only (3.4). It is
   reported with `created: false` and nothing in it changes.
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

Validation reads and never writes. In order: the workspace is a real directory
(not a symlink) whose real path is directly under the real workspaces root;
`.git` is a real directory and `git rev-parse --absolute-git-dir` names it;
origin is set exactly once and equals the expected URL; HEAD is
`refs/heads/<branch>` (an unborn branch is fine). The workspaces root itself
must be a real directory that resolves to `<real group dir>/task-workspaces`.

Every reason is in `ENV_BLOCK_REASONS`: the task is **blocked**, never
**failed**, maps to the `environment` class and spends no attempt.

| Reason | When |
| --- | --- |
| `env_workspace_unconfigured` | The agent group dir is unknown, or the task has no repository URL |
| `env_workspace_permission` | Creating or reading the workspace fails with `EACCES`, `EPERM` or `EROFS` |
| `env_workspace_invalid` | Not a directory, a symlink, resolves outside the root, not a git repository, or a malformed task id |
| `env_workspace_remote_mismatch` | Origin missing, different or set more than once, or a URL with credentials |
| `env_workspace_branch_mismatch` | HEAD is not the task branch (another branch, or detached) |
| `env_workspace_limit` | A new workspace would exceed `ARGUS_TASK_WORKSPACE_MAX` |

A missing repository lease keeps `env_repository_unavailable`; the workspace
step does not run then. An existing origin URL with userinfo is reported as
"origin carries credentials", not echoed.

# 4. Open Questions

- Retention: when should a finished task's workspace go, given that cleanup
  only acts on terminated instances?
- Byte quota: is a count cap enough, or does a large repository need a size
  limit per workspace or per group?
- Executor probe: `env_executor_auth` still needs a check that can run before the
  session container exists.
- P4: how an interrupted attempt resumes from the workspace, and what happens
  when the next attempt lands on another instance, which does not have the
  workspace.

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
