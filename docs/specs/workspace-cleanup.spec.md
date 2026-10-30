# Workspace cleanup: terminated NanoClaw instance directories

# 1. Purpose and Scope

Every NanoClaw instance gets a persistent working directory
`<nanoclaw.instances_root>/<instance id>` ([worker lifecycle](worker-lifecycle.spec.md)
3.8). Nothing removes it when the instance ends, so the directories of
terminated instances keep growing. On one host this reached about 209
directories and 617 GiB and filled the disk. Some of them held git work that
was never published.

This spec adds a conservative, opt-in cleanup. It deletes the directory of an
instance whose stored state is `terminated`, and only after any unpublished git
work in it has been archived outside the instances root and verified. It also
adds the audit trail, the operator surfaces (REST, MCP, dashboard) and a
restore tool.

# 2. Non-Goals

- No cleanup for any instance that is not stored as `terminated`. Being absent
  from a live listing, having a lost agent, an old age or an unknown folder is
  never enough.
- No Docker prune, no image, volume or network cleanup, no global cache prune.
- No cleanup on remote Docker daemons or Kubernetes. Argus only deletes paths on
  a filesystem it provably shares (`TargetInfo.shares_filesystem`).
- No archive of non-git content. NanoClaw state outside git repositories
  (conversations, logs, caches) is deleted with the directory.
- No rewrite of git history. Committed content goes into the bundle as it is.
- No new external service and no new dependency. The `git` CLI is required only
  when a repository has to be archived; without it the instance is blocked.

# 3. Decisions Made

## 3.1 Settings (off by default)

All settings are environment variables read by `Settings.from_env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARGUS_WORKSPACE_CLEANUP_ENABLED` | `false` | Runs the background loop and the termination trigger, and allows real runs |
| `ARGUS_WORKSPACE_CLEANUP_DRY_RUN` | `true` | The loop only reports; `false` lets it delete |
| `ARGUS_WORKSPACE_CLEANUP_ARCHIVE_ROOT` | unset | Where WIP archives go; without it WIP blocks and the run-now API refuses a real run |
| `ARGUS_WORKSPACE_CLEANUP_MIN_AGE_HOURS` | `168` | Minimum time since `terminated_at` |
| `ARGUS_WORKSPACE_CLEANUP_INTERVAL_S` | `3600` | Pause between loop runs; base of the retry backoff |
| `ARGUS_WORKSPACE_CLEANUP_RUN_TIMEOUT_S` | `1800` | Wall-clock cap of one run |
| `ARGUS_WORKSPACE_CLEANUP_MAX_INSTANCES` | `20` | Directories one run may act on |
| `ARGUS_WORKSPACE_CLEANUP_MAX_ARCHIVE_BYTES` | `2147483648` | Archive budget per instance |
| `ARGUS_WORKSPACE_CLEANUP_MAX_ATTEMPTS` | `3` | Attempts per directory before automatic runs back off |
| `ARGUS_WORKSPACE_CLEANUP_LOW_DISK_BYTES` | `10737418240` | Free-space floor (see 3.7) |
| `ARGUS_WORKSPACE_CLEANUP_GIT_TIMEOUT_S` | `300` | Cap of one git command |

The 7-day minimum age is a default that only matters once cleanup is enabled.
It is not a product rule; operators set their own.

## 3.2 What a run does

A run scans each validated instances root (3.3) and gives every entry exactly
one audit status:

- `preserved`: kept because it is not eligible. Reason codes: `not_uuid`,
  `not_directory`, `unknown_instance`, `pool_mismatch`, `not_terminated`,
  `too_recent`, `remote_target`, `root_invalid`.
- `eligible`: a dry run found nothing that stops the deletion. The reason says
  `wip_found` or `no_wip`.
- `blocked`: eligible, but a safety check stopped it. The directory is kept
  where it was (3.4–3.6). An unexpected error while the directory sits at its
  tombstone gives `internal_error`, and the directory is renamed back; one
  directory's error never stops the rest of the run. `permission_denied` means
  Argus cannot read the tree or cannot empty or remove a directory in it as its
  own uid (3.3); foreign-owned files do not cause it.
- `cleaned`: deleted, after its WIP was archived if it had any.
- `failed`: the deletion started and then hit an error. The tombstone (3.4) is
  kept and the next run finishes the deletion.

An instance is eligible only when all of these hold:

- its name is a canonical lowercase UUID;
- an `instances` row exists with that id, and its state is `terminated`;
- its pool uses the `nanoclaw` runtime, with an `instances_root` equal to the
  root being scanned;
- its pool's target is `docker` and `probe()` reports `shares_filesystem`
  (remote and Kubernetes targets are skipped with a reason);
- `terminated_at` is at least the minimum age in the past.

Rules for runs:

- **Order.** Candidates are processed oldest `terminated_at` first, at most
  `max_instances` per run, inside the run timeout.
- **Triggers.** A run's `trigger` is `loop`, `manual` or `terminated`:
  - `loop`: every `interval_s`. It is the fallback that finds every directory
    after a crash, a restart or a missed notification.
  - `manual`: the run-now API (3.8).
  - `terminated`: the reconciler calls `notify_terminated(instance_id)` once
    the write transaction that confirmed the instance `terminated` has
    committed and the pass has removed the exited containers. A rolled-back
    pass, `terminating`, and a disabled cleanup never notify. The call only sets
    a flag and never blocks the reconciler. A worker task then starts one
    run with the loop's `dry_run` setting. Notifications that arrive before that
    run starts coalesce into it. A notification during any run, or a run already
    in progress, gets exactly one follow-up run.

  The trigger only decides when a run starts. A `terminated` run scans the
  roots like any other run and applies every rule of this spec: eligibility,
  the minimum age, the lease, the tombstone and the reference checks. With the
  default minimum age the instance is `too_recent`, and a later loop run
  deletes it.
- **Retries.** Each lease increments `attempts` and sets `attempted_at` (3.4).
  An automatic run (`loop`, `terminated`) leaves a directory alone once
  `attempts` reaches `max_attempts`, until one of these holds:
  - **Backoff elapsed.** At least `min(interval_s * 2^(attempts -
    max_attempts), 24 h)` has passed since `attempted_at`. A row without
    `attempted_at` is due.
  - **Blocker changed.** The row's reason is one a read-only probe can see:
    `permission_denied`, `task_claimed`, `agent_active`, `shared_path`,
    `container_check_unavailable`, `container_mount`, `mounts_unreadable`,
    `mount_present`, `process_reference`, `low_disk` or `db_error`. The probe
    measures the tree, checks references and checks low disk, in the same
    order as a real run. If its result differs from the stored reason (or is
    empty), the directory is retried at once.

  A permanent failure therefore costs at most one attempt per backoff step, up
  to one a day, and never spins. A skipped directory keeps its last status and
  reason. Capped directories come after all others in the `max_instances`
  budget. A manual run always tries again.
- **Dry runs.** A dry run writes only audit rows and the run row. It does no
  rename, archive or delete. Git runs with `--no-optional-locks`.
- **Byte counts.** A dry run still counts bytes and finds WIP.

## 3.3 Path safety

**Instances root.** A root is used only if it passes all of these checks:

- absolute;
- an existing real directory, not a symlink;
- not `/`, and not a mount point;
- not equal to a system directory (`/usr`, `/var`, `/var/lib`, `/home`, `/tmp`,
  …) or to `$HOME`;
- not inside `/var/lib/docker`;
- it neither contains nor lies inside any of: the Argus data directory (DB,
  secrets, backups), the archive root, or any pool's `install_dir`,
  `agent_image_archive` or `docker_socket` (pool repositories are URLs, not
  paths).

A root that fails is reported once as `root_invalid` in the run's `skipped`
list. Nothing inside it is touched.

**Archive root.** It must be absolute, an existing real directory and not a
symlink. It must not be inside any instances root, and no instances root may be
inside it.

**Traversal.** All filesystem access is fd-relative:

- Every directory is opened with `O_DIRECTORY | O_NOFOLLOW` against its
  parent's fd and walked with `os.scandir(fd)`.
- Every removal is `unlink`/`rmdir` with `dir_fd`. A symlink is unlinked,
  never followed.
- An entry on a different `st_dev`, or listed in `/proc/self/mounts`, stops the
  deletion (`mount_present`).
- An entry that changed between `lstat` and `open` stops it too (`path_changed`).
- Names containing `/`, or equal to `.` or `..`, are never produced by `scandir`,
  and are rejected when they come from git output.

**Ownership pre-check.** Owning a path is not the same as being allowed to
delete it. Removing an entry needs write and search permission on its parent
directory, and emptying a directory needs read, write and search on that
directory. A foreign-owned file therefore never blocks a deletion. Argus blocks
only a directory it cannot empty or remove as its own uid.

A directory in the tree is removable by uid `U` when any of these holds:

- `U` is 0;
- the directory is owned by `U` (the deletion may `chmod` it);
- its owner bits are exactly `rwx` and the bits of the class that applies to
  `U` are `rwx`. That class is the group bits when the directory's gid is one of
  the supplementary or effective groups of Argus, otherwise the other bits.

An empty directory is exempt from this rule. It has nothing to empty, and
removing it is a write on its parent, which the rule checks. This covers the
mount point stubs described below. The deletion never `chmod`s an empty
directory, and never `chmod`s one it does not own.

If any directory fails this rule, the instance is `blocked` with
`permission_denied` and the fixed error detail `dir_not_writable`, before
anything moves. A directory Argus cannot open or list gives `permission_denied`
too. The check fails closed. No new reason code exists, and Argus never
changes the owner or mode of an existing directory to unblock it. Such a
directory is retried on the bounded schedule of 3.2.

**Root-owned session directories.** The NanoClaw host runs as
`nanoclaw.user` when set (socket mode), and Argus creates the instance
directory with that owner (`Mount.create_owner`). Inside it, NanoClaw
bind-mounts each session directory
`<instance>/data/v2-sessions/<agent>/sess-*` at `/workspace` in an agent
container. It also bind-mounts the group folder at `/workspace/agent`, and the
agent image has `WORKDIR /workspace/group`. The OCI runtime runs as root.
When a nested mount point or the workdir is missing it creates it inside the
session directory, so `sess-*/agent` and `sess-*/group` end up owned by
`0:0` with mode `0755`. The agent's `--user` cannot change that.
These stubs stay empty, so the rule above makes them removable.

The narrow, durable fix is outside Argus: NanoClaw's container runner would
create `agent/` and `group/` in each new session directory, as the host uid,
before `docker run`. Argus configures everything it owns: the host's user and
groups and the instance directory owner. A root-owned stub that is not empty,
for example a group workdir a root process wrote into, still blocks with
`permission_denied`.

## 3.4 Locking, revalidation and races

A real run handles each candidate in this order:

1. **DB lease.** In one write transaction the run re-reads the instance (still
   `terminated`) and takes a lease on the audit row (`lock_run_id`,
   `lock_expires_at = now + run timeout`). It also increments `attempts` and
   sets `attempted_at`. If
   another run holds an unexpired lease, the candidate is skipped and nothing
   is overwritten.
2. **Tombstone rename.** The directory is renamed atomically, inside the root,
   to `.argus-gc-<id>`. From then on a NanoClaw start for that id would get a
   fresh, empty directory, never the one being deleted. A missing source means
   another run already finished it, and the run moves on (idempotent).
3. **Revalidation, in a fresh DB read.** The instance must still be
   `terminated`. If it is not, the run renames the directory back and records
   it as `blocked` (`state_changed`). The run then checks references:
   - no agent of the instance is `active` or `draining`, and none holds a
     `claimed` task (`agent_active`, `task_claimed`);
   - no pool path (`instances_root`, `install_dir`, `agent_image_archive`,
     `docker_socket`) lies at or under the directory (`shared_path`);
   - no container on the shared Docker daemon, in any state, mounts a source at
     or under the directory, and no running container mounts one of its
     ancestors (`container_mount`). A stopped container that still mounts the
     directory blocks it too: it could be restarted. Remove the container to
     unblock;
   - `/proc/self/mounts` lists no mount at or under it (`mount_present`);
   - no process has its cwd or root inside it (`process_reference`).

   **Fail closed.** A DB error, a Docker listing error, or an unreadable
   `/proc/self/mounts` or `/proc` blocks the candidate. The reason codes are
   `db_error`, `container_check_unavailable` and `mounts_unreadable`. A single
   process whose cwd cannot be read (EACCES) is ignored, because unprivileged
   Argus cannot inspect it. This is a documented limitation.
4. **Archive.** WIP is archived (3.5). If that fails, the directory is renamed
   back and the instance is `blocked`.
5. **Delete.** The tombstone is deleted, and the lease is released along with
   the final status.

Concurrency and leftovers:

- A process-local lock lets only one run per process execute at a time. A
  second manual run gets `409 workspace_cleanup_running`. A loop run that
  finds the lock taken is skipped; a termination waits for it (3.2).
- Two runs in different objects or processes are serialized by the DB lease.
- On the next run, a leftover tombstone either finishes its deletion (the row
  is `failed`, or its archive is recorded) or is renamed back and treated
  normally. A tombstone whose row holds another run's unexpired lease is left
  alone.
- A run cancelled (timeout, shutdown) before deletion starts renames the
  directory back; once deletion has started the row says
  `delete_interrupted` and the next run finishes it.
- A run row left in `running` by a process that died is marked `interrupted`
  once it is older than the run timeout.

## 3.5 WIP preservation

**Finding repositories.** The run walks the directory and treats as a
repository any directory that contains `.git` (a directory, or a gitfile as
below), or that has `HEAD`, `objects/` and `refs/` (a bare repository). Each
directory is opened with `O_NOFOLLOW` and must be the inode its parent listed;
the walk never follows a symlink and never crosses onto another device
(`mount_present`). It does not descend into `.git`, nor into a regenerable
directory inside a repository's working tree, so deep `node_modules` or
`.cache` trees never count against the limits. Outside a repository a
regenerable name is walked like any other directory, so a repository under
`build/` is still found. The walk reads directory entries and gitfiles only,
never other file contents.

**Limits.** The walk stops at depth 64, 50 repositories or 1,000,000 directory
entries in total; the safe walker that measures and deletes stops at depth
256. Each gives `scan_limit`, with the limit named in the audit `error` field:
`depth`, `repo_count`, `entry_count` or `tree_depth`. Details are fixed codes,
never paths or contents.

**Gitfiles.** A `.git` *file* (a linked worktree, or a submodule's
`gitdir: ../.git/modules/<name>`) is read without following a symlink, as a
regular file of at most 4096 bytes holding one `gitdir: <path>` line. NanoClaw
creates worktrees inside its container, where the instance directory is
mounted at `/workspace/agent` (`CONTAINER_MOUNT`), so an absolute path is
resolved as follows:

- under `/workspace/agent`, that prefix is replaced by the scanned directory;
- under the scanned directory, it is used as it is;
- anything else is refused.

A relative path resolves from the gitfile's directory. The resolved git
directory must exist and stay inside the scanned directory, with no `..`
escape, no symlink at any component and no other device. A linked worktree's
`commondir` is checked the same way. Git always writes it as the relative
`../..`; an absolute one outside the scanned directory is refused, because git
rereads that file itself. Git then runs with explicit `--git-dir` and
`--work-tree`, never discovering a repository on its own, so a broken `.git`
cannot fall through to an enclosing one. Each repository's scan, bundle and
patches therefore cover its own HEAD and index, plus all shared refs. A
worktree is restored as a standalone repository. Any failed check blocks the
instance with `unsupported_repo` and a detail: `gitfile_type`,
`gitfile_invalid`, `gitfile_too_large`, `gitfile_unreadable`, `gitdir_outside`,
`gitdir_missing`, `gitdir_symlink`, `gitdir_unreadable`, or the same
`commondir_*` codes plus `commondir_absolute`.

**Unborn and broken repositories.** A repository with no commit yet (unborn
HEAD on a branch) has no bundle, but its staged, unstaged and untracked WIP is
archived, and restore points HEAD at that branch before applying the patches.
Objects that no ref reaches are not WIP and are not archived. Anything git
cannot read blocks the instance with `git_error` and is never taken as "no
WIP": a HEAD that is neither a commit nor a branch (detail `head_invalid`), a
ref naming a missing object, or a `.git` directory git rejects. A shallow
repository with WIP is `unsupported_repo` (`shallow`), because its bundle would
need commits the archive lacks. A bundle that lists prerequisites is refused
for the same reason (`bundle_not_self_contained`).

**Classification, per repository:**

- **Local commits:** `git rev-list --all --not --remotes`, which includes the
  stash and a detached HEAD. A branch, PR or succeeded task never counts as
  proof that work was published.
- **Staged changes:** `git diff --cached --binary`.
- **Unstaged changes:** `git diff --binary`.
- **Untracked files:** `git ls-files --others -z`, which does not apply
  `.gitignore`.
- **Regenerable directories** inside a working tree are excluded from
  untracked files, at any depth. The allowlist
  is exactly: `node_modules`, `.venv`, `venv`, `__pycache__`, `.cache`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.next`, `.turbo`,
  `target`, `dist`, `build`.
- **Sensitive paths** are never archived:
  - files: `.env*`, `*.pem`, `*.key`, `id_*`, `.netrc`, `.git-credentials`,
    `auth.json`, `credentials*`, `.credentials.json`;
  - anything under `.ssh`, `.aws`, `.gnupg`, `.config/gh`, `.docker` or
    `secrets`.

  This covers untracked files and the staged and unstaged diffs; the diffs are
  generated with exclude pathspecs. Each such path is listed in the manifest
  under `skipped_sensitive`, as a path only. A repository whose only changes
  are sensitive still gets a manifest, so the skip is recorded. If a local
  commit touches such a
  path, it is listed under `sensitive_in_commits`, and the bundle still carries
  it (no history rewrite).

**Archive contents.** A repository with any WIP gets the following under
`<archive_root>/<instance id>/<run id>/`:

- `repo-N.bundle`: `git bundle create --all`, self-contained, checked with
  `git bundle verify`;
- `repo-N.staged.patch` and `repo-N.unstaged.patch`;
- `repo-N.untracked.tar`, with regular files and symlinks only, each opened
  with `O_NOFOLLOW`;
- one `manifest.json` covering the whole instance.

**Manifest.** For every repository: its relative path, kind, HEAD, branch,
remote refs (names and SHAs, never URLs), counts, its artifacts (name, bytes,
sha256), and `skipped_sensitive`. For the whole archive: timestamps, plus the
instance and run ids.

**Writing the archive:**

1. Check that the archive root has at least `low_disk_bytes` plus the
   untracked bytes free (`archive_no_space` otherwise), then write everything
   into `<archive_root>/.tmp-<id>-<run>/`.
2. `fsync` every file and the directory.
3. Re-read each artifact and check its sha256 against what was written.
4. `rename` the directory into place and `fsync` its parent.

The source is deleted only after all of this succeeds.

**Failures block, never delete.** On any failure the run removes the temporary
directory, keeps the source, and records a reason code:

- `archive_error`, `archive_hash_mismatch`;
- `archive_permission`, `archive_no_space`;
- `archive_timeout`, `git_missing`, `git_error`;
- `archive_too_large`;
- `archive_root_unset`, `archive_root_invalid`.

Logs, audit rows and API responses carry reason codes and exception class
names only. They never carry file contents, git output or remote URLs.

**Restore.** `python -m argus_api.fleet.workspace_restore <archive dir> <dest>`
does the following:

1. Checks every artifact's hash against the manifest; an artifact name with
   `/` or a leading `.` is refused (`unsafe_path`).
2. For each repository, runs `git init` (bare for a bare repository) and
   fetches `refs/*:refs/*` from the bundle.
3. Points HEAD back at its branch or detached SHA.
4. Applies the staged patch with `--index`, then the unstaged patch.
5. Extracts the untracked tar into an empty destination, refusing absolute
   paths, `..`, and anything but regular files and symlinks. Files are created
   with `O_EXCL | O_NOFOLLOW` and no parent may be a symlink, so it never
   writes through a link. Symlinks are recreated as they were, wherever they
   point.

## 3.6 Byte accounting

- `bytes_estimated` is the sum of `st_blocks * 512` over the tree. It is
  deduplicated by `(st_dev, st_ino)`, so a hardlink counts once and a sparse
  file counts only its allocated blocks.
- `bytes_freed` is the `statvfs` free-space delta of the instances root's
  filesystem around the deletion, clamped at 0. It is approximate: other
  writers move it too.
- Neither number equals `du`. Reflinked or snapshotted extents, and hardlinks
  to files outside the tree, may free less than estimated.

## 3.7 Low disk

A run is in low-disk mode when the free space of the archive root, or of an
instances root, is below `low_disk_bytes`. In that mode:

- no archive is written and no instance directory is deleted;
- in an eligible instance where no repository has local commits, staged,
  unstaged or untracked changes, the run deletes only the allowlisted
  regenerable directories inside repository working trees, using the same safe
  walker;
- the instance stays `blocked` with reason `low_disk`, and the freed bytes are
  recorded.

Nothing outside eligible terminated instances is touched.

## 3.8 Audit, API and surfaces

**Migration.** `0048_workspace_cleanup.sql` adds two tables:

- `workspace_cleanup_runs`: one row per run, with its counts, bytes, free
  space before and after, `skipped` targets and pools, and sanitized error.
- `workspace_cleanup`: one row per `(root, name)`, with its status, reason,
  attempts, bytes, archive path, sanitized error, lease and timestamps.

`0054_workspace_cleanup_retry.sql` rebuilds `workspace_cleanup_runs` so that
its `trigger` check accepts `terminated`, copying every row. It also adds
`workspace_cleanup.attempted_at`, the time of the last lease, which the retry
backoff counts from (3.2).

**Events.** A real run records an event of each kind it produces. Each one
carries reason codes and byte counts:

- `workspace.cleaned`, `workspace.blocked` and `workspace.failed` (subject:
  the instance);
- `workspace_cleanup.finished` (subject: the run).

**REST, all under `/api/fleet/workspace-cleanup`:**

- `GET /fleet/workspace-cleanup` (managers): the settings in effect, the last
  run, counts per status, bytes estimated and freed, and the latest blocked or
  failed rows.
- `GET /fleet/workspace-cleanup/instances?status&limit&offset` (managers):
  audit rows with a total.
- `POST /fleet/workspace-cleanup/run` takes `{dry_run: true, wait_s: 0..30}`
  and returns the summary (as `GET`), whose `last_run` shows the run. The
  rules:
  - It starts the run in the background and waits up to `wait_s` for it.
  - Dry runs are open to managers, even while cleanup is disabled.
  - `dry_run: false` needs the admin scope (`403`), with cleanup enabled
    (`409 workspace_cleanup_disabled`) and a valid archive root
    (`409` with the code `archive_root_unset` or `archive_root_invalid`).

**MCP (managers).** Three tools forward to these routes:
`get_workspace_cleanup`, `list_workspace_cleanup` and `run_workspace_cleanup`
(`dry_run` defaults to true; REST enforces admin for a real run).

**Dashboard.** The Fleet view gets a "Workspace cleanup" card. It shows the
settings in effect, the last run, the counts, and estimated against freed bytes
(labelled approximate). It also has a "Dry run now" button.

The loop is not a readiness check, because its interval is long. A run that
fails is visible in its run row and events.

**Known limitations.** Git runs with hooks, fsmonitor, external diff and
textconv disabled, but clean/smudge filters configured by the repository
still run while it computes diffs. An unprivileged Argus cannot see the cwd of
other users' processes. Untracked files inside regenerable directories of a
working tree are not archived. The removability check reads mode bits and group
membership only; it ignores ACLs, immutable flags and read-only mounts, so a
directory it accepts can still fail at delete time (`permission_denied`).

# 4. Open Questions

- Should NanoClaw state outside git (group memory, conversation logs) be
  archived too? Today it is deleted with the directory.
- Should committed secrets block cleanup instead of being bundled as they are?
- Should an orphan folder with no instance row ever be eligible? It is not, by
  design. Today an operator removes it by hand.

# 5. Constraints

- Single node, SQLite only. Two migrations (0048, 0054), with no data
  dropped. All writes go through
  `Database.write()` with `tx.now`. No filesystem, git or Docker call happens
  inside a write transaction.
- Fail closed. When safety cannot be proven, the directory stays.
- A trigger never widens what is deleted. Only the rules of 3.2–3.4 decide, and
  a container missing from a listing never deletes anything.
- No secrets in logs, audit rows, events or responses.
- Deletion never leaves the validated instances root, and it never follows a
  symlink.
- English only.

# 6. Implementation Consequences

- **Backend:**
  - `apps/api/src/argus_api/fleet/workspace_cleanup.py`: the run, eligibility,
    lease, revalidation, safe walker, accounting and loop;
  - `fleet/workspace_archive.py`: the repository scan, archive and verify;
  - `fleet/workspace_restore.py`: restore and its CLI;
  - routes in `fleet/routes.py`;
  - models in `argus_core.fleet`;
  - `DockerTarget.mount_sources()`;
  - settings in `config.py`;
  - `fleet/reconciler.py`: an optional `on_terminated` callback on `run`,
    `reconcile` and `reconcile_pool`, called after commit;
  - the loop, the termination worker and the callback wired in `main.py`;
  - three tools in `mcp_server.py`.
- **Tests:** `apps/api/tests/test_workspace_cleanup.py`, which covers the
  filesystem, git, races and failure injection on real `tmp_path` trees, plus
  REST and MCP coverage. The dashboard card is tested in
  `apps/dashboard/src/views/Fleet.test.tsx`.
- **Operator guide:**
  [runbooks/2026-10-24.workspace-cleanup.ref.md](../runbooks/2026-10-24.workspace-cleanup.ref.md).
