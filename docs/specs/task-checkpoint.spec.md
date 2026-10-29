# Task checkpoint: keep a task's unpublished work across attempts and workers

# 1. Purpose and Scope

When an attempt of a `repository:write` task ends without success, its task
workspace ([task workspace](task-workspace.spec.md)) may hold work nobody pushed:
local commits, staged and unstaged changes, untracked files. A later attempt that
runs in another worker instance has a different group directory and would start
empty. This spec covers P4 of
[worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md) for the
workspace: the NanoClaw channel saves that work to a node-local, hash-verified
checkpoint and restores it into the next attempt's workspace before any model
starts, or blocks the task with a typed reason. A host path or a continuation note
is not a transfer; only a checkpoint that passes validation is.

A checkpoint reaches another instance only if both read the same store directory
with the same node id. Under the `nanoclaw` runtime Argus mounts no shared store
into host containers, so in practice a checkpoint serves retries on the instance
that wrote it (see 3.8).

The scope is `integrations/nanoclaw/argus-checkpoint.ts`, its calls in
`runTask` and the claim loop, and the `env_checkpoint_*` block reasons.

# 2. Non-Goals

- No cross-node transfer. No transport between nodes is proven; a checkpoint
  from another node is refused, never copied or guessed.
- No resume of the engine's session transcript, and no claim that an engine
  resumes. This spec moves files and commits only.
- No account rotation or credential handling. Account switching stays separate
  from session continuity ([account selection](account-selection.spec.md)).
- No network, fetch or push, and no use of any git host credential.
- No cleanup of live workspaces, no deploy, no operation on live instances.
- No encryption at rest beyond file permissions (3.6).

# 3. Decisions Made

## 3.1 Store and layout

The store is a directory on the node, set by `ARGUS_CHECKPOINT_DIR` (or the
`checkpointRoot` option). Unset means checkpoints are off: nothing is saved or
restored. Neither `install.sh` nor `provision.sh` sets it; a pool's `env` can.
Set `ARGUS_NODE_ID` with it: the hostname of a host container is its container id,
which changes when the container is recreated. The store root and each task
directory must be real directories owned by the worker user with no group or
other access; a symlink or a wider mode is `env_checkpoint_invalid`.

`<store>/<task id>/<checkpoint id>/` holds `manifest.json`, `work.bundle` (a git
bundle of all refs, when HEAD exists), `staged.patch` and `unstaged.patch`
(binary, byte-exact), and one copy per untracked file. A checkpoint is complete
only once its manifest exists: it is written in a temporary directory and renamed.
A directory without a manifest is ignored by restore and is never trusted.

## 3.2 Manifest and identity

The manifest records the format, task id, branch (`argus/task-<8hex>`), repository
URL (no credentials), node id (`ARGUS_NODE_ID`, else the hostname), attempt, HEAD,
the list of staged, unstaged and untracked paths, and the size and sha256 of every
file. Its own sha256 is the checkpoint reference
`checkpoint:<id>:<manifest sha256>`.

Restore verifies, in order: the manifest parses and is within limits; every file
exists with the recorded size and hash; task id, branch, repository URL and node id
equal the destination's; every path is relative, free of `..` and `.git`, and
every symlink target stays inside the workspace; when an expected reference is
given, its manifest digest matches. Any failure blocks.

## 3.3 Save

After an attempt that did not complete, and after its session closed, the channel
checkpoints the workspace if the task has `repository:write`. A clean workspace or
a missing one saves nothing. Saving is best effort: a failure is logged and never
changes the attempt's outcome. It is read-only on the workspace (git is run with
hooks and fsmonitor disabled).

Git runs on the host, but the workspace's `.git` is written by the worker, and
flags such as `--no-ext-diff` do not stop `git diff` from running a
`filter.<name>.clean` command. So before git touches a workspace, at save and at
restore, its `.git/config` must contain only keys on an allowlist of inert
settings (core basics, remote, branch, user, and similar). It must have no
`commondir` and no object alternates. Anything else, such as `filter.*`, `diff.*`,
`gpg.*`, `include.*`, `core.worktree` or `core.pager`, is `env_checkpoint_invalid`
and git is not run. A workspace set up with `git lfs install --local` is
therefore refused.

A nested repository, which git lists as an untracked directory, is not copied:
its directory is recorded in the manifest's `skipped_nested`, and the rest of the
work is saved.

## 3.4 Restore

For attempt 2 or later, before the workspace step, the channel restores the newest
valid checkpoint of the task into `<group>/task-workspaces/<task id>`:

- No checkpoint: nothing happens, the workspace step creates a fresh workspace.
- The workspace already equals the checkpoint: no-op (idempotent).
- The workspace is absent or pristine (no commits, no changes): the restored
  repository is built in a temporary directory, verified against the manifest
  (HEAD, staged, unstaged and untracked sets) and renamed into place.
- The workspace holds different work, or is on another branch:
  `env_checkpoint_conflict`. Nothing is replaced, merged or reset.

The restore never uses the wrong branch: HEAD is set to the task branch before
anything is applied, and the final state is compared with the manifest.

## 3.5 Concurrency and leases

Save and restore of one task take a lease file in the task directory (exclusive
create, a random holder id, a TTL of 120 s). A live lease held by another holder is
`env_checkpoint_busy`, which spends no attempt. An expired lease is taken over
by whoever first creates the takeover token named after it (exclusive create),
and is replaced by an atomic rename. The lease file is never absent during a
takeover, so of any number of racers exactly one wins. A token left by a taker
that died is itself expired after the TTL and is taken over the same way. The TTL
bounds a crashed holder, not a stalled one: a holder that outlives its TTL is not
detected (see 3.8).

## 3.6 Confidentiality and limits

Files named like credentials (`.env*`, `*.pem`, `*.key`, `id_*`, `.netrc`,
`.git-credentials`, `auth.json`, `credentials*`, and anything under `.ssh`,
`.aws`, `.gnupg`, `.config/gh`, `.docker`, `secrets`) are excluded from patches and
untracked copies and recorded by name only. They are not restored; the worker
re-provisions credentials through the gateway. Files are mode 0600 and
directories 0700. Credentials are never migrated.

The list matches exact names, case-sensitively, by file name only. Other places a
secret can sit are not covered: names such as `*.p12`, `.npmrc`, `.pypirc` or
`kubeconfig`, a secret inside an innocuous file, and anything committed or
stashed. The bundle carries every ref (`--all`, including `refs/stash`), so a
credential the worker committed or stashed is stored in the checkpoint. Its path
is listed in `sensitive_in_commits` when the name matches. The store is
therefore as confidential as the workspace itself.

Limit: `ARGUS_CHECKPOINT_MAX_BYTES` (default 256 MiB), at most 5000 untracked
files; over it the save fails `env_checkpoint_limit` and the workspace is left as
is. A restore is offline, so the bundle must hold the full history (`--all`), not
only the unpublished commits, and the limit applies to the whole repository. A
fixture with 64 MiB of incompressible history saved in about 2 s and restored in
about 1 s. With 300 MiB the save fails `env_checkpoint_limit`, after the bundle
was written to a temporary directory and removed. Each git step also has a fixed
30 s timeout, and a step that runs past it or past its output bound is also
`env_checkpoint_limit`.

Retention keeps the newest 3 checkpoints per task; older ones are removed after
the new one is written, never the new one itself.

## 3.7 Failure reasons

All are in `ENV_BLOCK_REASONS`: the task is **blocked** (the `environment` class),
and no attempt is spent.

| Reason | When |
| --- | --- |
| `env_checkpoint_unavailable` | An expected checkpoint is missing or incomplete, or it was written on another node |
| `env_checkpoint_invalid` | Hash, size, identity, path or permission validation fails |
| `env_checkpoint_conflict` | The workspace holds different work, or is on another branch |
| `env_checkpoint_busy` | Another holder has the task's lease |
| `env_checkpoint_limit` | The work exceeds the byte or file limit |

## 3.8 Verified capabilities and limits

Verified by `argus-checkpoint.test.ts`, with real git, disposable fixtures and two
group directories sharing only the store: commits, staged, unstaged (including
binary) and untracked work restore with identical hashes and task identity;
unborn branches; idempotent and concurrent restores; refusal of tampered,
truncated, incomplete, foreign-task, foreign-repository and foreign-node
checkpoints; unsafe paths; open permissions and symlinks; conflicts; every listed
sensitive name excluded whether staged, unstaged or untracked, at any depth;
workspace git config that would run a command on the host refused at save and at
restore, without running it; three racers on an expired lease with one winner,
and takeover of a token left by a dead taker; a git timeout reported as
`env_checkpoint_limit` with no partial checkpoint; a nested repository skipped
and named. `argus-accounts.test.ts` verifies that a `/complete` refused by the
API (409) still saves a checkpoint, since the attempt is marked complete only
after the API accepts it.

Not verified and not promised: lease renewal for an operation longer than the
TTL; transfer between nodes or hosts; a store shared between `nanoclaw` host
containers (none is mounted); resuming an engine session; restore after the store
disk is lost. The checkpoint reference is logged but not yet reported to Argus as
a `wip` observation.

Known limit: a checkpoint outlives the work being published. If attempt 1 saves
a checkpoint and attempt 2 pushes its work and then fails, attempt 2 saves
nothing (its workspace is clean), and attempt 3 finds attempt 1's checkpoint and
a workspace that differs from it: `env_checkpoint_conflict`, and the task blocks.
Removing the task's checkpoint directory in the store unblocks it. Deleting
checkpoints on a clean save is not done, because with a shared store it could
delete another instance's unpublished work (see 4).

# 4. Open Questions

- Which proven transport (object store, rsync over the fleet network) would make
  a cross-node restore safe, and who authorizes it?
- Should the reference be reported through the continuation `wip` field so the
  next attempt pins the exact checkpoint?
- Should a task that finishes remove its checkpoints at once, instead of waiting
  for retention?
- How should a checkpoint be superseded once its work is published (3.8)? For
  example, record the pushed HEAD so that a checkpoint whose commits are all
  reachable from it is treated as done.
- Should the store be a shared mount for `nanoclaw` host containers, with a
  stable `ARGUS_NODE_ID` per node, so a retry on another instance can restore?

# 5. Constraints

- Single node ([single-node](single-node.spec.md)): local filesystem only, no new
  service, no schema change.
- No network call and no credential in any step.
- Never delete or overwrite work in a live workspace.
- English only, no secrets in the repository.

# 6. Implementation Consequences

- `integrations/nanoclaw/argus-checkpoint.ts`: `checkpointTaskWorkspace`,
  `restoreTaskWorkspace`, `checkpointRef`, `parseCheckpointRef`, `isSensitive`,
  with `argus-checkpoint.test.ts`. `argus-workspace.ts` exports `defaultGit` and
  `hasCredentials`.
- `integrations/nanoclaw/argus.ts`: `workspacePreflight` restores before preparing
  the workspace; the claim loop calls `checkpointAfterAttempt` after the session
  closes; new option `checkpointRoot`.
- `install.sh` copies the module and test; `provision.sh check` runs the test.
- `packages/core/src/argus_core/tasks.py`: five reasons join `ENV_BLOCK_REASONS`;
  the dashboard inbox labels them; [failure classes](failure-classes.spec.md) 3.3
  lists them.
