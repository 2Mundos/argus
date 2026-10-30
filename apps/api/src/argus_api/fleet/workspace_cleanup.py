"""Cleanup of terminated NanoClaw instance directories (workspace-cleanup.spec.md).

Off by default. A run scans each validated instances root, records one audit
row per entry, and deletes a directory only when its instance is stored as
`terminated`, every reference check passes under a DB lease and a tombstone
rename, and its unpublished git work is archived and verified. Anything that
cannot be proven safe is kept. Filesystem and git work runs in worker threads;
no filesystem, git or Docker call happens inside a write transaction.
"""

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.fleet import (
    Pool,
    WorkspaceCleanupEntry,
    WorkspaceCleanupPage,
    WorkspaceCleanupRun,
    WorkspaceCleanupSettings,
    WorkspaceCleanupStatus,
    WorkspaceCleanupSummary,
)

from argus_api.config import Settings
from argus_api.db import Database, Tx, ts
from argus_api.errors import ConflictError
from argus_api.events import record
from argus_api.fleet import runtimes, service
from argus_api.fleet import workspace_archive as wa
from argus_api.fleet.targets import Target, TargetError, TargetRegistry

log = logging.getLogger(__name__)

ACTOR = "workspace-cleanup"
TOMBSTONE = ".argus-gc-"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_TREE_DEPTH = 256
TARGET_TIMEOUT_S = 30.0
FIRST_LOOP_DELAY_S = 60.0
MAX_RETRY_BACKOFF_S = 24 * 3600.0
SYSTEM_DIRS = frozenset(
    {
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",  # noqa: S108 - a path refused as a root, not a temp file
        "/usr",
        "/var",
        "/var/lib",
        "/var/log",
        "/var/tmp",  # noqa: S108
    }
)
# Deleting started: a leftover tombstone with this reason is finished, not restored.
DELETING = ("delete_interrupted", "delete_error")
# Blockers a read-only probe can see clear or change (spec 3.2, retries).
PROBED = frozenset(
    {
        "permission_denied",
        "task_claimed",
        "agent_active",
        "shared_path",
        "container_check_unavailable",
        "container_mount",
        "mounts_unreadable",
        "mount_present",
        "process_reference",
        "low_disk",
        "db_error",
    }
)
S = WorkspaceCleanupStatus


class CleanupRunningError(ConflictError):
    code = "workspace_cleanup_running"


class Unsafe(Exception):
    """A safety check failed; `code` is the audit reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Paths


def _norm(path: str) -> str:
    return os.path.normpath(path)  # pure string work, no I/O


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _unescape(field_: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field_)


def read_mounts(path: str = "/proc/self/mounts") -> list[str]:
    """Mount points from the kernel's table; OSError when it cannot be read."""
    with open(path, "rb") as f:
        data = f.read()
    points = []
    for line in data.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            points.append(_unescape(fields[1].decode("utf-8", "surrogateescape")))
    return points


def _load_mounts(mounts: Callable[[], list[str]] | None) -> list[str]:
    """Current mount table, or mounts_unreadable when the table cannot be read."""
    try:
        return read_mounts() if mounts is None else mounts()
    except OSError as exc:
        raise Unsafe("mounts_unreadable") from exc


def _mount_at_or_under(abs_path: str, mounts: list[str]) -> bool:
    """True when abs_path is a mount point or a mount sits at/under it."""
    return any(_within(m, abs_path) for m in mounts)


def check_root(root: str, forbidden: list[str], mounts: list[str]) -> str | None:
    """None when `root` may hold deletable instance directories, else why not."""
    if root in ("", "/") or not os.path.isabs(root):
        return "not_absolute_or_slash"
    if os.path.normpath(root) != root:
        return "not_normalized"
    if os.path.realpath(root) != root:
        return "symlink"
    try:
        st = os.lstat(root)
    except OSError:
        return "missing"
    if not stat.S_ISDIR(st.st_mode):
        return "not_directory"
    home = os.environ.get("HOME")
    if root in SYSTEM_DIRS or (home and os.path.normpath(home) == root):
        return "system_directory"
    if _within(root, "/var/lib/docker"):
        return "docker_data"
    if os.path.ismount(root) or root in mounts:
        return "mountpoint"
    for path in forbidden:
        if _within(path, root) or _within(root, path):
            return "overlaps_protected_path"
    return None


def check_archive_root(archive: str | None, roots: list[str]) -> str | None:
    if not archive:
        return "archive_root_unset"
    if (
        not os.path.isabs(archive)
        or os.path.normpath(archive) != archive
        or os.path.realpath(archive) != archive
        or not os.path.isdir(archive)
        or os.path.islink(archive)
    ):
        return "archive_root_invalid"
    if any(_within(archive, r) or _within(r, archive) for r in roots):
        return "archive_root_invalid"
    return None


def free_bytes(path: str) -> int | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def _open_dir(
    name: str, dir_fd: int | None, expect: os.stat_result | None = None
) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    if expect is not None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (expect.st_dev, expect.st_ino):
            os.close(fd)
            raise Unsafe("path_changed")
    return fd


@dataclass
class Tree:
    bytes: int = 0
    unremovable_dir: bool = False


def _can_remove_dir(st: Any, uid: int, groups: Collection[int]) -> bool:
    """Whether uid can empty and remove this directory (spec 3.3); fails closed.

    A foreign directory cannot be chmod-ed, so it needs rwx for the owner and
    for the class that applies to uid: group if st_gid is in groups, else other.
    An empty one needs nothing of its own (callers skip this check for it):
    removing it is a write on its parent, which is checked as a directory too.
    """
    if uid == 0 or st.st_uid == uid:
        return True
    mode = stat.S_IMODE(st.st_mode)
    if (mode >> 6) & 0o7 != 0o7:
        return False
    shift = 3 if st.st_gid in groups else 0
    return (mode >> shift) & 0o7 == 0o7


def measure(root: str, name: str, uid: int) -> Tree:
    """Allocated bytes under root/name, each inode once; never follows symlinks.

    Raises Unsafe for a mount point, a changed path or an unreadable directory.
    """
    rfd = _open_dir(root, None)
    try:
        st = os.lstat(name, dir_fd=rfd)
        if not stat.S_ISDIR(st.st_mode):
            raise Unsafe("path_changed")
        tree = Tree()
        _measure(rfd, name, st, st.st_dev, uid, set(), tree, 0, os.getgroups())
        return tree
    except PermissionError as exc:
        raise Unsafe("permission_denied") from exc
    finally:
        os.close(rfd)


def _measure(
    parent_fd: int,
    name: str,
    st: os.stat_result,
    dev: int,
    uid: int,
    seen: set[tuple[int, int]],
    tree: Tree,
    depth: int,
    groups: Collection[int] = (),
) -> None:
    if depth > MAX_TREE_DEPTH:
        raise Unsafe("scan_limit", "tree_depth")
    if st.st_dev != dev:
        raise Unsafe("mount_present")
    key = (st.st_dev, st.st_ino)
    if key not in seen:
        seen.add(key)
        tree.bytes += st.st_blocks * 512
    if not stat.S_ISDIR(st.st_mode):
        return
    fd = _open_dir(name, parent_fd, st)
    try:
        with os.scandir(fd) as it:
            entries = list(it)
        # An empty foreign directory, such as a mount point stub the container
        # runtime created as root, is removed by its parent alone.
        if entries and not _can_remove_dir(st, uid, groups):
            tree.unremovable_dir = True
        for e in entries:
            _measure(
                fd,
                e.name,
                e.stat(follow_symlinks=False),
                dev,
                uid,
                seen,
                tree,
                depth + 1,
                groups,
            )
    finally:
        os.close(fd)


def remove_tree(
    root: str,
    name: str,
    dev: int,
    mounts: Callable[[], list[str]] | None = None,
) -> None:
    """Delete root/name with fd-relative unlink/rmdir; symlinks are unlinked.

    Re-reads the mount table at each directory so same-device bind mounts and
    mounts added after revalidation still raise `mount_present` (fail-closed
    when the table cannot be read).
    """
    rfd = _open_dir(root, None)
    try:
        _remove(rfd, name, os.path.join(root, name), dev, mounts, 0)
    finally:
        os.close(rfd)


def _remove(
    parent_fd: int,
    name: str,
    abs_path: str,
    dev: int,
    mounts: Callable[[], list[str]] | None,
    depth: int,
) -> None:
    st = os.lstat(name, dir_fd=parent_fd)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    if st.st_dev != dev:
        raise Unsafe("mount_present")
    if _mount_at_or_under(abs_path, _load_mounts(mounts)):
        raise Unsafe("mount_present")
    if depth > MAX_TREE_DEPTH:
        raise Unsafe("scan_limit", "tree_depth")
    fd = _open_dir(name, parent_fd, st)
    try:
        with os.scandir(fd) as it:
            names = [e.name for e in it]
        if names and (st.st_mode & 0o700) != 0o700:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) | 0o700)
        for child in names:
            _remove(
                fd,
                child,
                os.path.join(abs_path, child),
                dev,
                mounts,
                depth + 1,
            )
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def prune_caches(
    root: str,
    name: str,
    caches: list[str],
    dev: int,
    mounts: Callable[[], list[str]] | None = None,
) -> None:
    """Delete only the given allowlisted cache directories under root/name."""
    rfd = _open_dir(root, None)
    try:
        for rel in caches:
            parts = rel.split("/")
            if parts[-1] not in wa.REGENERABLE or ".." in parts or "" in parts:
                raise Unsafe("path_changed")
            fd = _open_dir(name, rfd)
            try:
                for part in parts[:-1]:
                    nxt = _open_dir(part, fd)
                    os.close(fd)
                    fd = nxt
                if os.fstat(fd).st_dev != dev:
                    raise Unsafe("mount_present")
                cache_abs = os.path.join(root, name, *parts)
                if _mount_at_or_under(cache_abs, _load_mounts(mounts)):
                    raise Unsafe("mount_present")
                _remove(fd, parts[-1], cache_abs, dev, mounts, 0)
            finally:
                os.close(fd)
    finally:
        os.close(rfd)


def process_refs(proc: Path, paths: list[str]) -> bool:
    """Whether any visible process has its cwd or root inside one of `paths`.

    OSError when /proc cannot be listed; a single unreadable process is skipped
    (an unprivileged Argus cannot inspect other users' processes).
    """
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        for link in ("cwd", "root"):
            try:
                target = os.readlink(proc / pid / link)
            except OSError:
                continue
            if any(_within(target, p) for p in paths):
                return True
    return False


# ---------------------------------------------------------------------------
# Runs


@dataclass
class Candidate:
    root: str
    name: str  # the instance id
    pool: Pool
    target: Target
    resume: bool = False  # a tombstone whose deletion already started
    deleting: bool = False  # set once this run starts deleting


@dataclass
class RunState:
    id: str
    trigger: str
    actor: str
    dry_run: bool
    low_disk: bool = False
    counts: dict[S, int] = field(default_factory=lambda: dict.fromkeys(S, 0))
    scanned: int = 0
    bytes_estimated: int = 0
    bytes_freed: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    status: S
    reason: str
    bytes_estimated: int | None = None
    bytes_freed: int | None = None
    archive_path: str | None = None
    error: str | None = None


class WorkspaceCleaner:
    """Runs cleanups one at a time per process; the DB lease covers the rest."""

    def __init__(
        self,
        db: Database,
        targets: TargetRegistry,
        settings: Settings,
        *,
        mounts: Callable[[], list[str]] = read_mounts,
        proc: Path = Path("/proc"),
        git: wa.Git | None = None,
        writer: wa.ArchiveWriter | None = None,
        uid: int | None = None,
    ) -> None:
        self.db = db
        self.targets = targets
        self.settings = settings
        self.mounts = mounts
        self.proc = proc
        self.git = git or wa.Git(timeout_s=settings.workspace_cleanup_git_timeout_s)
        self.writer = writer or wa.ArchiveWriter()
        self.uid = os.geteuid() if uid is None else uid
        self.busy = False
        self.current: asyncio.Task[WorkspaceCleanupRun] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._terminated = asyncio.Event()  # a termination not yet run for
        # Set by each run's plan: local Docker targets and the archive root check.
        self._local_targets: list[Target] = []
        self._archive_problem: str | None = None

    # -- entry points -------------------------------------------------------

    def start(self, trigger: str, actor: str, dry_run: bool) -> asyncio.Task:
        """Start a run in the background; raises CleanupRunningError if one runs."""
        self._claim()
        self.current = asyncio.create_task(
            self._run(trigger, actor, dry_run), name="workspace-cleanup"
        )
        self.current.add_done_callback(_log_failure)
        return self.current

    async def run(self, trigger: str, actor: str, dry_run: bool) -> WorkspaceCleanupRun:
        self._claim()
        return await self._run(trigger, actor, dry_run)

    async def loop(self) -> None:
        await asyncio.sleep(
            min(FIRST_LOOP_DELAY_S, self.settings.workspace_cleanup_interval_s)
        )
        while True:
            try:
                await self.run("loop", ACTOR, self.settings.workspace_cleanup_dry_run)
            except CleanupRunningError:
                pass
            except Exception:
                log.exception("workspace cleanup run failed")
            await asyncio.sleep(self.settings.workspace_cleanup_interval_s)

    def notify_terminated(self, instance_id: str) -> None:
        """The reconciler committed `instance_id` as terminated. Never blocks:
        it only flags a run for `follow_terminations`, so any number of calls
        before that run starts coalesce into it."""
        if self.settings.workspace_cleanup_enabled:
            log.debug("workspace cleanup requested for %s", instance_id)
            self._terminated.set()

    async def follow_terminations(self) -> None:
        """One `terminated` run per batch of notifications. A notification
        during any run, or a run already in progress, gets one follow-up."""
        while True:
            await self._terminated.wait()
            await self._idle.wait()
            self._terminated.clear()
            try:
                await self.run(
                    "terminated", ACTOR, self.settings.workspace_cleanup_dry_run
                )
            except CleanupRunningError:
                self._terminated.set()  # claimed meanwhile: run after it
            except Exception:
                log.exception("workspace cleanup run failed")

    def _claim(self) -> None:
        if self.busy:
            raise CleanupRunningError("a workspace cleanup run is already in progress")
        self.busy = True
        self._idle.clear()

    async def _run(
        self, trigger: str, actor: str, dry_run: bool
    ) -> WorkspaceCleanupRun:
        s = self.settings
        run = RunState(uuid4().hex, trigger, actor, dry_run)
        error: str | None = None
        status = "finished"
        free_before: int | None = None
        try:
            async with self.db.write() as tx:
                stale = ts(
                    tx.now - timedelta(seconds=s.workspace_cleanup_run_timeout_s)
                )
                await tx.conn.execute(
                    "UPDATE workspace_cleanup_runs SET status = 'interrupted',"
                    " finished_at = ? WHERE status = 'running' AND started_at < ?",
                    (ts(tx.now), stale),
                )
                await tx.conn.execute(
                    "INSERT INTO workspace_cleanup_runs (id, trigger, actor, dry_run,"
                    " status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                    (run.id, trigger, actor, int(dry_run), ts(tx.now)),
                )
            self.git.deadline = time.monotonic() + s.workspace_cleanup_run_timeout_s
            roots: list[str] = []
            try:
                async with asyncio.timeout(s.workspace_cleanup_run_timeout_s):
                    roots, candidates = await self._plan(run)
                    free_before = self._free(roots)
                    for cand in candidates:
                        try:
                            await self._process(run, cand)
                        except Exception as exc:
                            # One directory's bug must not stop the others; the
                            # lease expires and a later run retries it.
                            # Sanitize: never log exception messages (may carry paths).
                            log.error(
                                "workspace cleanup of %s failed (%s)",
                                cand.name,
                                wa.sanitize(exc),
                            )
                            run.skipped.append(f"{cand.name}: internal_error")
            except TimeoutError:
                status, error = "timed_out", "TimeoutError"
            except Exception as exc:
                log.error(
                    "workspace cleanup run %s failed (%s)",
                    run.id,
                    wa.sanitize(exc),
                )
                status, error = "failed", wa.sanitize(exc)
            free_after = self._free(roots)
        finally:
            self.busy = False
            self._idle.set()
        async with self.db.write() as tx:
            await tx.conn.execute(
                "UPDATE workspace_cleanup_runs SET status = ?, finished_at = ?,"
                " low_disk = ?, scanned = ?, eligible = ?, preserved = ?, blocked = ?,"
                " cleaned = ?, failed = ?, bytes_estimated = ?, bytes_freed = ?,"
                " free_before = ?, free_after = ?, skipped = ?, error = ? WHERE id = ?",
                (
                    status,
                    ts(tx.now),
                    int(run.low_disk),
                    run.scanned,
                    run.counts[S.ELIGIBLE],
                    run.counts[S.PRESERVED],
                    run.counts[S.BLOCKED],
                    run.counts[S.CLEANED],
                    run.counts[S.FAILED],
                    run.bytes_estimated,
                    run.bytes_freed,
                    free_before,
                    free_after,
                    json.dumps(run.skipped),
                    error,
                    run.id,
                ),
            )
            if not dry_run:
                await record(
                    tx,
                    actor,
                    "workspace_cleanup",
                    run.id,
                    "workspace_cleanup.finished",
                    {
                        "status": status,
                        "cleaned": run.counts[S.CLEANED],
                        "blocked": run.counts[S.BLOCKED],
                        "failed": run.counts[S.FAILED],
                        "bytes_estimated": run.bytes_estimated,
                        "bytes_freed": run.bytes_freed,
                        "low_disk": run.low_disk,
                    },
                )
        async with self.db.read() as conn:
            found = await get_run(conn, run.id)
        assert found is not None
        return found

    @staticmethod
    def _free(roots: list[str]) -> int | None:
        seen: dict[int, int] = {}
        for r in roots:
            with contextlib.suppress(OSError):
                dev = os.stat(r).st_dev
                free = free_bytes(r)
                if free is not None:
                    seen[dev] = free
        return sum(seen.values()) if seen else None

    # -- planning -------------------------------------------------------------

    async def _plan(self, run: RunState) -> tuple[list[str], list[Candidate]]:
        s = self.settings
        async with self.db.read() as conn:
            pools = await service.list_pools(conn)
            async with conn.execute(
                "SELECT id, pool, state, terminated_at FROM instances"
            ) as cur:
                instances = {r["id"]: dict(r) for r in await cur.fetchall()}
            async with conn.execute(
                "SELECT root, name, status, reason, attempts, attempted_at,"
                " lock_run_id, lock_expires_at FROM workspace_cleanup"
            ) as cur:
                audit = {(r["root"], r["name"]): dict(r) for r in await cur.fetchall()}
        by_name = {p.name: p for p in pools}
        try:
            mounts = await asyncio.to_thread(self.mounts)
        except OSError:
            run.skipped.append("all roots: mounts_unreadable")
            return [], []

        # Which pools sit on a filesystem this process provably shares.
        local: dict[str, Target] = {}
        for pool in pools:
            if pool.target != "docker":
                continue
            target = self.targets.get(pool.target, *runtimes.target_location(pool))
            try:
                info = await asyncio.wait_for(target.probe(), TARGET_TIMEOUT_S)
            except (TargetError, TimeoutError):
                continue
            if info.shares_filesystem:
                local[pool.name] = target
        self._local_targets = list({id(t): t for t in local.values()}.values())
        nanoclaw = [p for p in pools if p.runtime == "nanoclaw" and p.nanoclaw]
        for p in nanoclaw:
            if p.name not in local:
                run.skipped.append(f"pool {p.name}: remote_target")

        archive = s.workspace_cleanup_archive_root
        forbidden = [
            str(s.data_dir.resolve()),
            str(s.secrets_path.resolve()),
            *([archive] if archive else []),
            *_pool_paths(pools, include_roots=False),
        ]
        roots: list[str] = []
        local_roots = {
            _norm(p.nanoclaw.instances_root)
            for p in nanoclaw
            if p.nanoclaw and p.name in local
        }
        for root in sorted(local_roots):
            why = check_root(root, forbidden, mounts)
            if why:
                run.skipped.append(f"root {root}: root_invalid ({why})")
            else:
                roots.append(root)
        self._archive_problem = check_archive_root(archive, roots)
        if not run.dry_run and self._archive_problem:
            run.skipped.append(f"archive root: {self._archive_problem}")
        now = self.db.now()
        low = s.workspace_cleanup_low_disk_bytes
        for path in [
            *roots,
            *([archive] if archive and not self._archive_problem else []),
        ]:
            free = free_bytes(path)
            if free is None or free < low:
                run.low_disk = True

        cutoff = ts(now - timedelta(hours=s.workspace_cleanup_min_age_hours))
        candidates: list[tuple[str, Candidate]] = []
        capped: list[tuple[dict, Candidate]] = []
        rows: list[tuple[str, str, str | None, str | None, Outcome]] = []
        for root in roots:
            if not run.dry_run:
                await self._settle_tombstones(root, ts(now))
            try:
                entries = await asyncio.to_thread(_list_dir, root)
            except OSError:
                run.skipped.append(f"root {root}: root_invalid (unreadable)")
                continue
            for name, is_dir in entries:
                resume = False
                if name.startswith(TOMBSTONE):
                    row = audit.get((root, name.removeprefix(TOMBSTONE)))
                    if run.dry_run or row is None or row["reason"] not in DELETING:
                        continue  # left for the next real run to settle
                    name, resume = name.removeprefix(TOMBSTONE), True
                run.scanned += 1
                inst = instances.get(name)
                pool = by_name.get(inst["pool"]) if inst else None

                reason: str | None = None
                if not UUID_RE.match(name):
                    reason = "not_uuid"
                elif not is_dir:
                    reason = "not_directory"
                elif inst is None or pool is None:
                    reason = "unknown_instance"
                elif (
                    pool.runtime != "nanoclaw"
                    or pool.nanoclaw is None
                    or _norm(pool.nanoclaw.instances_root) != root
                ):
                    reason = "pool_mismatch"
                elif pool.name not in local:
                    reason = "remote_target"
                elif inst["state"] != "terminated":
                    reason = "not_terminated"
                elif inst["terminated_at"] is None or inst["terminated_at"] > cutoff:
                    reason = "too_recent"
                if reason or inst is None or pool is None:
                    outcome = Outcome(S.PRESERVED, reason or "unknown_instance")
                    pool_name = inst["pool"] if inst else None
                    rows.append(
                        (root, name, name if inst else None, pool_name, outcome)
                    )
                    continue
                prev = audit.get((root, name))
                cand = Candidate(root, name, pool, local[pool.name], resume)
                if (
                    run.trigger != "manual"
                    and prev is not None
                    and prev["status"] != S.CLEANED
                    and prev["attempts"] >= s.workspace_cleanup_max_attempts
                ):
                    capped.append((prev, cand))
                    continue
                candidates.append((inst["terminated_at"], cand))
        for root, name, instance_id, pool_name, outcome in rows:
            prev = audit.get((root, name))
            if (
                prev
                and prev["status"] == outcome.status
                and prev["reason"] == outcome.reason
            ):
                run.counts[outcome.status] += 1
                await self._touch(run, root, name)
                continue
            await self._finish(
                run, root, name, instance_id, pool_name, outcome, event=False
            )
        candidates.sort(key=lambda c: c[0])
        chosen = [c for _, c in candidates]
        for prev, cand in capped:  # after every directory not yet capped
            if await self._retry_due(run, cand, prev):
                chosen.append(cand)
            else:  # keeps its last blocker and the attempted_at the backoff runs from
                run.counts[S(prev["status"])] += 1
                await self._touch(run, cand.root, cand.name)
        return roots, chosen[: s.workspace_cleanup_max_instances]

    async def _retry_due(self, run: RunState, cand: Candidate, prev: dict) -> bool:
        """Whether an automatic run retries a directory past `max_attempts`: once
        its backoff elapsed, or at once when a probe shows its blocker changed."""
        s = self.settings
        last = service.parse_ts(prev["attempted_at"])
        if last is None:
            return True
        over = min(prev["attempts"] - s.workspace_cleanup_max_attempts, 16)
        backoff = min(s.workspace_cleanup_interval_s * 2**over, MAX_RETRY_BACKOFF_S)
        if (self.db.now() - last).total_seconds() >= backoff:
            return True
        if cand.resume or prev["reason"] not in PROBED:
            return False
        return await self._probe(run, cand) != prev["reason"]

    async def _probe(self, run: RunState, cand: Candidate) -> str | None:
        """Read-only: the blocker `_real` would meet first, in its order."""
        try:
            tree = await asyncio.to_thread(measure, cand.root, cand.name, self.uid)
        except Unsafe as exc:
            return exc.code
        except OSError:
            return "permission_denied"
        if tree.unremovable_dir:
            return "permission_denied"
        path = os.path.join(cand.root, cand.name)
        if problem := await self._references(cand, [path]):
            return problem
        return "low_disk" if run.low_disk else None

    async def _settle_tombstones(self, root: str, now: str) -> None:
        """Rename back a leftover tombstone whose deletion never started and
        that no live lease holds (another process may be working on it).

        The lease is read fresh for each tombstone, never from the run's scan
        snapshot: a concurrent run leases and renames after that snapshot."""
        try:
            entries = await asyncio.to_thread(_list_dir, root)
        except OSError:
            return
        for name, _ in entries:
            if not name.startswith(TOMBSTONE):
                continue
            original = name.removeprefix(TOMBSTONE)
            if not UUID_RE.match(original):
                continue
            async with (
                self.db.read() as conn,
                conn.execute(
                    "SELECT reason, lock_run_id, lock_expires_at FROM workspace_cleanup"
                    " WHERE root = ? AND name = ?",
                    (root, original),
                ) as cur,
            ):
                row = await cur.fetchone()
            if row and row["reason"] in DELETING:
                continue
            if row and row["lock_run_id"] and (row["lock_expires_at"] or "") > now:
                continue
            await asyncio.to_thread(_restore_tombstone, root, name, original)

    # -- one candidate ----------------------------------------------------------

    async def _process(self, run: RunState, cand: Candidate) -> None:
        if run.dry_run:
            outcome = await self._dry(run, cand)
            await self._finish(
                run, cand.root, cand.name, cand.name, cand.pool.name, outcome
            )
            return
        prev = await self._lease(run, cand)
        if prev is None:
            return  # another run holds it, or it is already cleaned
        tomb = TOMBSTONE + cand.name
        if cand.resume:
            outcome = await self._resume(run, cand, tomb)
        else:
            outcome = await self._real(run, cand, tomb, prev)
        if outcome is None:
            return
        await self._finish(
            run, cand.root, cand.name, cand.name, cand.pool.name, outcome
        )

    async def _dry(self, run: RunState, cand: Candidate) -> Outcome:
        path = os.path.join(cand.root, cand.name)
        problem = await self._references(cand, [path])
        if problem:
            return Outcome(S.BLOCKED, problem)
        try:
            tree = await asyncio.to_thread(measure, cand.root, cand.name, self.uid)
        except Unsafe as exc:
            return Outcome(S.BLOCKED, exc.code, error=exc.detail or None)
        except OSError as exc:
            return Outcome(S.BLOCKED, "permission_denied", error=wa.sanitize(exc))
        if tree.unremovable_dir:
            return Outcome(
                S.BLOCKED, "permission_denied", tree.bytes, error="dir_not_writable"
            )
        try:
            found = await asyncio.to_thread(wa.scan, Path(path), self.git)
        except wa.ArchiveError as exc:
            return Outcome(S.BLOCKED, exc.code, tree.bytes, error=exc.detail or None)
        if run.low_disk:
            return Outcome(S.BLOCKED, "low_disk", tree.bytes)
        if found.has_wip and self._archive_problem:
            return Outcome(S.BLOCKED, self._archive_problem, tree.bytes)
        run.bytes_estimated += tree.bytes
        return Outcome(
            S.ELIGIBLE, "wip_found" if found.has_wip else "no_wip", tree.bytes
        )

    async def _real(
        self, run: RunState, cand: Candidate, tomb: str, prev: dict
    ) -> Outcome | None:
        root, name = cand.root, cand.name
        # Ownership / readability before anything moves (spec 3.3).
        try:
            tree = await asyncio.to_thread(measure, root, name, self.uid)
        except Unsafe as exc:
            return Outcome(S.BLOCKED, exc.code, error=exc.detail or None)
        except OSError as exc:
            return Outcome(S.BLOCKED, "permission_denied", error=wa.sanitize(exc))
        if tree.unremovable_dir:
            return Outcome(
                S.BLOCKED, "permission_denied", tree.bytes, error="dir_not_writable"
            )
        try:
            rfd = _open_dir(root, None)
        except OSError as exc:
            return Outcome(S.BLOCKED, "root_invalid", error=wa.sanitize(exc))
        try:
            try:
                os.rename(name, tomb, src_dir_fd=rfd, dst_dir_fd=rfd)
            except FileNotFoundError:
                await self._release(run, cand, prev)
                return None  # someone else finished it: nothing to do
            except OSError as exc:
                return Outcome(S.BLOCKED, "path_changed", error=wa.sanitize(exc))
            try:
                outcome = await self._locked(run, cand, tomb)
            except asyncio.CancelledError:
                if not cand.deleting:
                    with contextlib.suppress(OSError):
                        _rename_back(rfd, tomb, name)
                raise
            except Exception as exc:
                log.error(
                    "workspace cleanup of %s failed (%s)",
                    name,
                    wa.sanitize(exc),
                )
                outcome = (
                    Outcome(S.FAILED, "delete_error", error=wa.sanitize(exc))
                    if cand.deleting
                    else Outcome(S.BLOCKED, "internal_error", error=wa.sanitize(exc))
                )
            if outcome.status in (S.BLOCKED, S.PRESERVED):
                try:
                    _rename_back(rfd, tomb, name)
                except OSError as exc:
                    outcome.reason = "path_changed"
                    outcome.error = wa.sanitize(exc)
            return outcome
        finally:
            os.close(rfd)

    async def _locked(self, run: RunState, cand: Candidate, tomb: str) -> Outcome:
        """The directory is at its tombstone: revalidate, archive, delete."""
        root = cand.root
        tomb_path = os.path.join(root, tomb)
        problem = await self._revalidate(
            cand, [os.path.join(root, cand.name), tomb_path]
        )
        if problem:
            return Outcome(S.BLOCKED, problem)
        try:
            tree = await asyncio.to_thread(measure, root, tomb, self.uid)
        except Unsafe as exc:
            return Outcome(S.BLOCKED, exc.code, error=exc.detail or None)
        except OSError as exc:
            return Outcome(S.BLOCKED, "permission_denied", error=wa.sanitize(exc))
        if tree.unremovable_dir:
            return Outcome(
                S.BLOCKED, "permission_denied", tree.bytes, error="dir_not_writable"
            )
        try:
            found = await asyncio.to_thread(wa.scan, Path(tomb_path), self.git)
        except wa.ArchiveError as exc:
            return Outcome(S.BLOCKED, exc.code, tree.bytes, error=exc.detail or None)
        run.bytes_estimated += tree.bytes
        if run.low_disk:
            return await self._low_disk(run, cand, tomb, found, tree)
        archive_path: str | None = None
        if found.has_wip:
            if self._archive_problem:
                return Outcome(S.BLOCKED, self._archive_problem, tree.bytes)
            assert self.settings.workspace_cleanup_archive_root
            try:
                final = await asyncio.to_thread(
                    wa.archive,
                    Path(tomb_path),
                    found,
                    Path(self.settings.workspace_cleanup_archive_root),
                    cand.name,
                    run.id,
                    ts(self.db.now()),
                    self.git,
                    max_bytes=self.settings.workspace_cleanup_max_archive_bytes,
                    min_free=self.settings.workspace_cleanup_low_disk_bytes,
                    writer=self.writer,
                )
            except wa.ArchiveError as exc:
                return Outcome(
                    S.BLOCKED, exc.code, tree.bytes, error=exc.detail or None
                )
            archive_path = str(final)
        # Deletion starts: from here a leftover tombstone is finished, never restored.
        cand.deleting = True
        await self._mark_deleting(run, cand, tree.bytes, archive_path)
        return await self._delete(run, cand, tomb, tree.bytes, archive_path)

    async def _resume(self, run: RunState, cand: Candidate, tomb: str) -> Outcome:
        root = cand.root
        problem = await self._revalidate(
            cand, [os.path.join(root, cand.name), os.path.join(root, tomb)]
        )
        if problem:
            return Outcome(S.FAILED, problem)
        # Reuse the archive path recorded when deletion started so a later
        # finish keeps reason=archived and events say archived=true.
        archive_path: str | None = None
        estimated: int | None = None
        async with (
            self.db.read() as conn,
            conn.execute(
                "SELECT archive_path, bytes_estimated FROM workspace_cleanup"
                " WHERE root = ? AND name = ?",
                (root, cand.name),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is not None:
            archive_path = row["archive_path"]
            estimated = row["bytes_estimated"]
        return await self._delete(run, cand, tomb, estimated, archive_path)

    async def _delete(
        self,
        run: RunState,
        cand: Candidate,
        tomb: str,
        estimated: int | None,
        archive_path: str | None,
    ) -> Outcome:
        root = cand.root
        before = free_bytes(root)
        try:
            dev = os.lstat(root).st_dev
            await asyncio.to_thread(remove_tree, root, tomb, dev, self.mounts)
        except Unsafe as exc:
            return Outcome(
                S.FAILED,
                "delete_error",
                estimated,
                archive_path=archive_path,
                error=" ".join(filter(None, (exc.code, exc.detail))),
            )
        except OSError as exc:
            return Outcome(
                S.FAILED,
                "delete_error",
                estimated,
                archive_path=archive_path,
                error=wa.sanitize(exc),
            )
        after = free_bytes(root)
        freed = (
            max(0, after - before) if before is not None and after is not None else 0
        )
        run.bytes_freed += freed
        reason = "archived" if archive_path else "no_wip"
        return Outcome(S.CLEANED, reason, estimated, freed, archive_path)

    async def _low_disk(
        self, run: RunState, cand: Candidate, tomb: str, found: wa.Scan, tree: Tree
    ) -> Outcome:
        """No archive and no full delete; prune caches only when nothing is unique."""
        if found.has_wip or not found.cache_dirs:
            return Outcome(S.BLOCKED, "low_disk", tree.bytes)
        root = cand.root
        before = free_bytes(root)
        try:
            dev = os.lstat(root).st_dev
            await asyncio.to_thread(
                prune_caches, root, tomb, found.cache_dirs, dev, self.mounts
            )
        except Unsafe as exc:
            return Outcome(
                S.BLOCKED,
                "low_disk",
                tree.bytes,
                error=" ".join(filter(None, (exc.code, exc.detail))),
            )
        except OSError as exc:
            return Outcome(S.BLOCKED, "low_disk", tree.bytes, error=wa.sanitize(exc))
        after = free_bytes(root)
        freed = (
            max(0, after - before) if before is not None and after is not None else 0
        )
        run.bytes_freed += freed
        return Outcome(S.BLOCKED, "low_disk", tree.bytes, freed)

    # -- checks -------------------------------------------------------------------

    async def _revalidate(self, cand: Candidate, paths: list[str]) -> str | None:
        """Fresh DB read under the tombstone: state and references."""
        try:
            async with (
                self.db.read() as conn,
                conn.execute(
                    "SELECT state FROM instances WHERE id = ?", (cand.name,)
                ) as cur,
            ):
                row = await cur.fetchone()
        except Exception:
            return "db_error"
        if row is None or row["state"] != "terminated":
            return "state_changed"
        return await self._references(cand, paths)

    async def _references(self, cand: Candidate, paths: list[str]) -> str | None:
        try:
            async with self.db.read() as conn:
                async with conn.execute(
                    "SELECT a.state, (SELECT count(*) FROM tasks t WHERE"
                    " t.state = 'claimed' AND t.assignee_agent_id = a.id) AS claimed"
                    " FROM agents a WHERE a.instance_id = ?",
                    (cand.name,),
                ) as cur:
                    agents = await cur.fetchall()
                pools = await service.list_pools(conn)
        except Exception:
            return "db_error"
        if any(a["claimed"] for a in agents):
            return "task_claimed"
        if any(a["state"] in ("active", "draining") for a in agents):
            return "agent_active"
        for p in _pool_paths(pools, include_roots=True):
            if any(_within(p, path) for path in paths):
                return "shared_path"
        for target in self._local_targets:
            lister = getattr(target, "mount_sources", None)
            if lister is None:
                return "container_check_unavailable"
            try:
                sources = await asyncio.wait_for(lister(), TARGET_TIMEOUT_S)
            except (TargetError, TimeoutError):
                return "container_check_unavailable"
            for m in sources:
                src = _norm(m.source)
                if any(_within(src, path) for path in paths):
                    return "container_mount"
                if m.running and any(_within(path, src) for path in paths):
                    return "container_mount"
        try:
            mounts = await asyncio.to_thread(self.mounts)
        except OSError:
            return "mounts_unreadable"
        if any(_within(m, path) for m in mounts for path in paths):
            return "mount_present"
        try:
            busy = await asyncio.to_thread(process_refs, self.proc, paths)
        except OSError:
            return "mounts_unreadable"
        return "process_reference" if busy else None

    # -- audit ----------------------------------------------------------------------

    async def _lease(self, run: RunState, cand: Candidate) -> dict | None:
        """Take the audit row's lease if the instance is still terminated; return
        the previous status and reason, or None to skip the candidate."""
        s = self.settings
        async with self.db.write() as tx:
            async with tx.conn.execute(
                "SELECT state FROM instances WHERE id = ?", (cand.name,)
            ) as cur:
                inst = await cur.fetchone()
            async with tx.conn.execute(
                "SELECT status, reason, lock_run_id, lock_expires_at FROM workspace_cleanup"
                " WHERE root = ? AND name = ?",
                (cand.root, cand.name),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                if row["status"] == S.CLEANED:
                    return None
                held = row["lock_run_id"] and row["lock_run_id"] != run.id
                if held and row["lock_expires_at"] > ts(tx.now):
                    return None
            if inst is None or inst["state"] != "terminated":
                await _put(
                    tx,
                    run.id,
                    cand.root,
                    cand.name,
                    instance_id=cand.name,
                    pool=cand.pool.name,
                    status=S.BLOCKED,
                    reason="state_changed",
                )
                run.counts[S.BLOCKED] += 1
                return None
            expires = tx.now + timedelta(seconds=s.workspace_cleanup_run_timeout_s)
            prev = {"status": row["status"], "reason": row["reason"]} if row else None
            await _put(
                tx,
                run.id,
                cand.root,
                cand.name,
                attempts=1,
                instance_id=cand.name,
                pool=cand.pool.name,
                status=row["status"] if row else S.ELIGIBLE,
                reason=row["reason"] if row else "in_progress",
                lock_run_id=run.id,
                lock_expires_at=ts(expires),
                attempted_at=ts(tx.now),
            )
        return prev or {"status": S.ELIGIBLE, "reason": "in_progress"}

    async def _release(self, run: RunState, cand: Candidate, prev: dict) -> None:
        async with self.db.write() as tx:
            await _put(
                tx,
                run.id,
                cand.root,
                cand.name,
                status=prev["status"],
                reason=prev["reason"],
                lock_run_id=None,
                lock_expires_at=None,
            )

    async def _mark_deleting(
        self, run: RunState, cand: Candidate, estimated: int, archive_path: str | None
    ) -> None:
        async with self.db.write() as tx:
            await _put(
                tx,
                run.id,
                cand.root,
                cand.name,
                status=S.FAILED,
                reason="delete_interrupted",
                bytes_estimated=estimated,
                archive_path=archive_path,
                error=None,
            )

    async def _touch(self, run: RunState, root: str, name: str) -> None:
        async with self.db.write() as tx:
            await tx.conn.execute(
                "UPDATE workspace_cleanup SET last_run_id = ?, updated_at = ?"
                " WHERE root = ? AND name = ?",
                (run.id, ts(tx.now), root, name),
            )

    async def _finish(
        self,
        run: RunState,
        root: str,
        name: str,
        instance_id: str | None,
        pool: str | None,
        outcome: Outcome,
        *,
        event: bool = True,
    ) -> None:
        run.counts[outcome.status] += 1
        async with self.db.write() as tx:
            cols: dict[str, Any] = {
                "instance_id": instance_id,
                "pool": pool,
                "status": outcome.status,
                "reason": outcome.reason,
                "error": outcome.error,
                "lock_run_id": None,
                "lock_expires_at": None,
            }
            if outcome.bytes_estimated is not None:
                cols["bytes_estimated"] = outcome.bytes_estimated
            if outcome.bytes_freed is not None:
                cols["bytes_freed"] = outcome.bytes_freed
            if outcome.archive_path is not None:
                cols["archive_path"] = outcome.archive_path
            if outcome.status == S.CLEANED:
                cols["cleaned_at"] = ts(tx.now)
            await _put(tx, run.id, root, name, **cols)
            if (
                event
                and not run.dry_run
                and instance_id
                and outcome.status
                in (
                    S.CLEANED,
                    S.BLOCKED,
                    S.FAILED,
                )
            ):
                await record(
                    tx,
                    run.actor,
                    "instance",
                    instance_id,
                    f"workspace.{outcome.status}",
                    {
                        "run_id": run.id,
                        "reason": outcome.reason,
                        "bytes_estimated": outcome.bytes_estimated,
                        "bytes_freed": outcome.bytes_freed,
                        "archived": outcome.archive_path is not None,
                    },
                )


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("workspace cleanup run failed (%s)", wa.sanitize(exc))


def _exists(name: str, dir_fd: int) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def _rename_back(rfd: int, tomb: str, name: str) -> None:
    if _exists(name, rfd):
        raise FileExistsError(errno.EEXIST, "exists")
    os.rename(tomb, name, src_dir_fd=rfd, dst_dir_fd=rfd)


def _restore_tombstone(root: str, tomb: str, original: str) -> None:
    with contextlib.suppress(OSError):
        rfd = _open_dir(root, None)
        try:
            if not _exists(original, rfd):
                os.rename(tomb, original, src_dir_fd=rfd, dst_dir_fd=rfd)
        finally:
            os.close(rfd)


def _list_dir(root: str) -> list[tuple[str, bool]]:
    rfd = _open_dir(root, None)
    try:
        with os.scandir(rfd) as it:
            return sorted((e.name, e.is_dir(follow_symlinks=False)) for e in it)
    finally:
        os.close(rfd)


def _pool_paths(pools: list[Pool], *, include_roots: bool) -> list[str]:
    paths: list[str] = []
    for p in pools:
        n = p.nanoclaw
        if n is None:
            continue
        candidates = [n.install_dir, n.agent_image_archive, n.docker_socket]
        if include_roots:
            candidates.append(n.instances_root)
        paths += [os.path.normpath(c) for c in candidates if c]
    return paths


async def _put(
    tx: Tx, run_id: str, root: str, name: str, *, attempts: int = 0, **cols: Any
) -> None:
    """Upsert one audit row; `attempts` is added to the stored count."""
    cols = {**cols, "last_run_id": run_id, "updated_at": ts(tx.now)}
    names = list(cols)
    await tx.conn.execute(
        "INSERT INTO workspace_cleanup (root, name, first_seen_at, attempts, "  # noqa: S608 - code-controlled columns
        + ", ".join(names)
        + ") VALUES (?, ?, ?, ?, "
        + ", ".join("?" for _ in names)
        + ") ON CONFLICT (root, name) DO UPDATE SET attempts = attempts + excluded.attempts, "
        + ", ".join(f"{n} = excluded.{n}" for n in names),
        (
            root,
            name,
            ts(tx.now),
            attempts,
            *(str(v) if isinstance(v, S) else v for v in cols.values()),
        ),
    )


# ---------------------------------------------------------------------------
# Reads for the API


def _entry(row: aiosqlite.Row) -> WorkspaceCleanupEntry:
    return WorkspaceCleanupEntry.model_validate(dict(row))


def _run(row: aiosqlite.Row) -> WorkspaceCleanupRun:
    data = dict(row)
    data["dry_run"] = bool(data["dry_run"])
    data["low_disk"] = bool(data["low_disk"])
    data["skipped"] = json.loads(data["skipped"])
    return WorkspaceCleanupRun.model_validate(data)


async def get_run(
    conn: aiosqlite.Connection, run_id: str
) -> WorkspaceCleanupRun | None:
    async with conn.execute(
        "SELECT * FROM workspace_cleanup_runs WHERE id = ?", (run_id,)
    ) as cur:
        row = await cur.fetchone()
    return _run(row) if row else None


async def list_entries(
    conn: aiosqlite.Connection, status: S | None, limit: int, offset: int
) -> WorkspaceCleanupPage:
    wanted = str(status) if status else None
    async with conn.execute(
        "SELECT count(*) AS n FROM workspace_cleanup WHERE ? IS NULL OR status = ?",
        (wanted, wanted),
    ) as cur:
        row = await cur.fetchone()
    total = row["n"] if row else 0
    async with conn.execute(
        "SELECT * FROM workspace_cleanup WHERE ? IS NULL OR status = ?"
        " ORDER BY updated_at DESC, name LIMIT ? OFFSET ?",
        (wanted, wanted, limit, offset),
    ) as cur:
        items = [_entry(r) for r in await cur.fetchall()]
    return WorkspaceCleanupPage(total=total, items=items)


def cleanup_settings(s: Settings) -> WorkspaceCleanupSettings:
    return WorkspaceCleanupSettings(
        enabled=s.workspace_cleanup_enabled,
        dry_run=s.workspace_cleanup_dry_run,
        archive_root=s.workspace_cleanup_archive_root,
        min_age_hours=s.workspace_cleanup_min_age_hours,
        interval_s=s.workspace_cleanup_interval_s,
        run_timeout_s=s.workspace_cleanup_run_timeout_s,
        max_instances=s.workspace_cleanup_max_instances,
        max_archive_bytes=s.workspace_cleanup_max_archive_bytes,
        max_attempts=s.workspace_cleanup_max_attempts,
        low_disk_bytes=s.workspace_cleanup_low_disk_bytes,
    )


async def summary(
    conn: aiosqlite.Connection, settings: Settings, running: bool
) -> WorkspaceCleanupSummary:
    async with conn.execute(
        "SELECT * FROM workspace_cleanup_runs ORDER BY started_at DESC, id LIMIT 1"
    ) as cur:
        last = await cur.fetchone()
    counts = dict.fromkeys(S, 0)
    estimated = freed = 0
    async with conn.execute(
        "SELECT status, count(*) AS n, sum(coalesce(bytes_estimated, 0)) AS est,"
        " sum(coalesce(bytes_freed, 0)) AS freed FROM workspace_cleanup GROUP BY status"
    ) as cur:
        for r in await cur.fetchall():
            counts[S(r["status"])] = r["n"]
            if r["status"] == S.CLEANED:
                freed += r["freed"]
            elif r["status"] != S.PRESERVED:
                estimated += r["est"]
    async with conn.execute(
        "SELECT * FROM workspace_cleanup WHERE status IN ('blocked', 'failed')"
        " ORDER BY updated_at DESC, name LIMIT 10"
    ) as cur:
        problems = [_entry(r) for r in await cur.fetchall()]
    return WorkspaceCleanupSummary(
        settings=cleanup_settings(settings),
        running=running,
        last_run=_run(last) if last else None,
        counts=counts,
        bytes_estimated=estimated,
        bytes_freed=freed,
        problems=problems,
    )
