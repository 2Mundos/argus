"""Workspace cleanup of terminated NanoClaw instances (workspace-cleanup.spec.md).

Every test works on real directories and real git repositories under tmp_path.
Pools carry a short placeholder instances root (the option is length-limited);
`service.list_pools` is patched to swap in the tmp_path root.
"""

import asyncio
import errno
import json
import logging
import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import BinaryIO

import pytest
from api_support import FakeTarget, insert_agent
from argus_api.config import Settings
from argus_api.db import Database, ts
from argus_api.fleet import service
from argus_api.fleet import workspace_archive as wa
from argus_api.fleet import workspace_cleanup as wc
from argus_api.fleet.targets import TargetRegistry
from argus_api.fleet.workspace_restore import main as restore_main
from argus_api.fleet.workspace_restore import restore
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.fleet import NanoclawOptions, PoolUpsert
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from test_mcp import tool

PLACEHOLDER = "/srv/wc-a"
SECRET = "sk-test-SECRETVALUE-1234567890"
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return out.stdout.decode()


class Env:
    def __init__(self, tmp_path: Path, db: Database) -> None:
        self.tmp = tmp_path
        self.db = db
        self.root = tmp_path / "instances"
        self.root.mkdir()
        self.archive = tmp_path / "archive"
        self.archive.mkdir()
        self.proc = tmp_path / "proc"
        self.proc.mkdir()
        self.mounts: list[str] = []
        self.target = FakeTarget()
        self.target.shares_filesystem = True
        self.registry = TargetRegistry()
        self.registry.override("docker", None, self.target)

    def settings(self, **kw) -> Settings:
        base: dict = {
            "data_dir": self.tmp / "data",
            "workspace_cleanup_enabled": True,
            "workspace_cleanup_dry_run": False,
            "workspace_cleanup_archive_root": str(self.archive),
            "workspace_cleanup_min_age_hours": 0,
            "workspace_cleanup_low_disk_bytes": 0,
        }
        return Settings(**{**base, **kw})

    def cleaner(self, **kw) -> wc.WorkspaceCleaner:
        opts = {k: kw.pop(k) for k in ("git", "writer", "proc", "mounts") if k in kw}
        opts.setdefault("proc", self.proc)
        opts.setdefault("mounts", lambda: list(self.mounts))
        return wc.WorkspaceCleaner(self.db, self.registry, self.settings(**kw), **opts)

    async def run(self, dry_run: bool = False, **kw):
        return await self.cleaner(**kw).run("manual", "admin:test", dry_run)

    async def instance(self, iid: str, state: str = "terminated") -> Path:
        async with self.db.write() as tx:
            await tx.conn.execute(
                "INSERT INTO instances (id, pool, provider_ref, state, created_at,"
                " terminated_at) VALUES (?, 'nc', ?, ?, ?, ?)",
                (
                    iid,
                    f"ref-{iid}",
                    state,
                    ts(tx.now),
                    ts(tx.now) if state == "terminated" else None,
                ),
            )
        path = self.root / iid
        path.mkdir()
        (path / "notes.txt").write_text("hello")
        return path

    async def entry(self, name: str) -> dict:
        async with (
            self.db.read() as conn,
            conn.execute(
                "SELECT * FROM workspace_cleanup WHERE name = ?", (name,)
            ) as cur,
        ):
            row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def add_pool(db: Database, name: str = "nc", **nanoclaw) -> None:
    opts = {
        "install_dir": "/opt/nc",
        "instances_root": PLACEHOLDER,
        "agent_image": "a:1",
    }
    async with db.write() as tx:
        await service.upsert_pool(
            tx,
            name,
            PoolUpsert(
                runtime="nanoclaw",
                image="h",
                max_replicas=1,
                nanoclaw=NanoclawOptions(**{**opts, **nanoclaw}),
            ),
            "admin:test",
        )


@pytest.fixture
async def env(tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch) -> Env:
    e = Env(tmp_path, db)
    real = service.list_pools

    async def swapped(conn, *args, **kwargs):
        pools = await real(conn, *args, **kwargs)
        out = []
        for p in pools:
            n = p.nanoclaw
            if n is not None and n.instances_root == PLACEHOLDER:
                n = n.model_copy(update={"instances_root": str(e.root)})
                p = p.model_copy(update={"nanoclaw": n})
            out.append(p)
        return out

    monkeypatch.setattr(service, "list_pools", swapped)
    await add_pool(db)
    return e


def make_wip_repo(tmp: Path, work: Path) -> Path:
    """A clone with one pushed commit, one local commit, staged, unstaged and
    untracked changes, and an untracked secret."""
    origin = tmp / "origin.git"
    git(tmp, "init", "-q", "--bare", str(origin))
    repo = work / "repo"
    git(tmp, "clone", "-q", str(origin), str(repo))
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "pushed")
    git(repo, "push", "-q", "origin", "HEAD:main")
    (repo / "b.txt").write_text("local\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "local only")
    (repo / "staged.txt").write_text("staged\n")
    git(repo, "add", "staged.txt")
    (repo / "a.txt").write_text("one\nunstaged\n")
    (repo / "new.bin").write_bytes(bytes(range(256)))
    (repo / ".env").write_text(f"TOKEN={SECRET}\n")
    return repo


# -- happy path and dry run --------------------------------------------------------


async def test_terminated_directory_is_deleted(env: Env) -> None:
    path = await env.instance(A)
    run = await env.run()
    assert run.status == "finished"
    assert run.cleaned == 1
    assert not path.exists()
    assert not any(env.root.iterdir())
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("cleaned", "no_wip")
    assert row["bytes_estimated"] > 0
    async with (
        env.db.read() as conn,
        conn.execute("SELECT kind FROM events WHERE subject_id = ?", (A,)) as cur,
    ):
        kinds = [r["kind"] for r in await cur.fetchall()]
    assert kinds == ["workspace.cleaned"]


async def test_dry_run_deletes_nothing(env: Env) -> None:
    path = await env.instance(A)
    make_wip_repo(env.tmp, path)
    run = await env.run(dry_run=True)
    assert run.eligible == 1 and run.cleaned == 0
    assert (path / "notes.txt").exists()
    assert sorted(p.name for p in env.root.iterdir()) == [A]
    assert not any(env.archive.iterdir())
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("eligible", "wip_found")


async def test_rerun_is_idempotent(env: Env) -> None:
    await env.instance(A)
    await env.run()
    second = await env.run()
    assert second.cleaned == 0 and second.failed == 0
    assert (await env.entry(A))["status"] == "cleaned"


# -- what is preserved -----------------------------------------------------------------


@pytest.mark.parametrize("state", ["reserved", "running", "terminating"])
async def test_live_states_are_preserved(env: Env, state: str) -> None:
    path = await env.instance(A, state)
    await env.run()
    assert (path / "notes.txt").exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("preserved", "not_terminated")


async def test_draining_instance_is_preserved(env: Env) -> None:
    path = await env.instance(A, "running")
    async with env.db.write() as tx:
        await tx.conn.execute("UPDATE instances SET draining = 1 WHERE id = ?", (A,))
    await env.run()
    assert path.exists()


async def test_unknown_odd_and_recent_entries_are_preserved(env: Env) -> None:
    orphan = env.root / B  # a UUID with no instance row
    orphan.mkdir()
    odd = env.root / "not-a-uuid"
    odd.mkdir()
    (env.root / "11111111-1111-4111-8111-11111111111f").write_text("file")
    recent = await env.instance(A)
    await env.run(workspace_cleanup_min_age_hours=1)
    assert orphan.exists() and odd.exists() and recent.exists()
    assert (await env.entry(B))["reason"] == "unknown_instance"
    assert (await env.entry("not-a-uuid"))["reason"] == "not_uuid"
    assert (await env.entry("11111111-1111-4111-8111-11111111111f"))[
        "reason"
    ] == "not_directory"
    assert (await env.entry(A))["reason"] == "too_recent"


async def test_remote_target_is_skipped(env: Env) -> None:
    env.target.shares_filesystem = False
    path = await env.instance(A)
    run = await env.run()
    assert path.exists()
    assert "pool nc: remote_target" in run.skipped


async def test_disabled_archive_root_blocks_wip(env: Env) -> None:
    path = await env.instance(A)
    make_wip_repo(env.tmp, path)
    await env.run(workspace_cleanup_archive_root=None)
    assert (path / "repo" / ".env").exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "archive_root_unset")


# -- path safety ------------------------------------------------------------------------


async def test_symlinked_instance_directory_is_not_followed(env: Env) -> None:
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    async with env.db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO instances (id, pool, provider_ref, state, created_at,"
            " terminated_at) VALUES (?, 'nc', 'r', 'terminated', ?, ?)",
            (A, ts(tx.now), ts(tx.now)),
        )
    (env.root / A).symlink_to(outside)
    await env.run()
    assert (outside / "keep.txt").read_text() == "keep"
    assert (env.root / A).is_symlink()
    assert (await env.entry(A))["reason"] == "not_directory"


async def test_symlinks_inside_the_tree_are_unlinked_not_followed(env: Env) -> None:
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    path = await env.instance(A)
    (path / "link").symlink_to(outside)
    (path / "filelink").symlink_to(outside / "keep.txt")
    run = await env.run()
    assert run.cleaned == 1
    assert (outside / "keep.txt").read_text() == "keep"


async def test_mount_inside_the_tree_blocks_and_keeps_the_directory(env: Env) -> None:
    path = await env.instance(A)
    (path / "mnt").mkdir()
    env.mounts = [str(path / "mnt")]
    await env.run()
    assert (path / "notes.txt").exists()
    assert (await env.entry(A))["reason"] == "mount_present"


async def test_unreadable_mount_table_fails_closed(env: Env) -> None:
    path = await env.instance(A)

    def broken() -> list[str]:
        raise OSError(errno.EACCES, "denied")

    run = await env.run(mounts=broken)
    assert path.exists()
    assert "all roots: mounts_unreadable" in run.skipped


async def test_measure_refuses_another_device(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    st = os.lstat(tmp_path / "d")
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(wc.Unsafe) as exc:
            wc._measure(fd, "d", st, st.st_dev + 1, 0, set(), wc.Tree(), 0)
    finally:
        os.close(fd)
    assert exc.value.code == "mount_present"


def test_root_validation(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert wc.check_root("/", [], []) == "not_absolute_or_slash"
    assert wc.check_root("relative/x", [], []) == "not_absolute_or_slash"
    assert wc.check_root(str(real) + "/../real", [], []) == "not_normalized"
    assert wc.check_root(str(link), [], []) == "symlink"
    assert wc.check_root(str(tmp_path / "missing"), [], []) == "missing"
    assert wc.check_root("/usr", [], []) == "system_directory"
    assert wc.check_root(str(real), [], [str(real)]) == "mountpoint"
    assert (
        wc.check_root(str(real), [str(real / "data")], []) == "overlaps_protected_path"
    )
    assert wc.check_root(str(real), [str(tmp_path)], []) == "overlaps_protected_path"
    assert wc.check_root(str(real), [], []) is None


async def test_symlinked_root_is_refused(env: Env, monkeypatch) -> None:
    path = await env.instance(A)
    link = env.tmp / "rootlink"
    link.symlink_to(env.root)
    env.root = link
    run = await env.run()
    assert path.exists()
    assert any("root_invalid (symlink)" in s for s in run.skipped)


def test_archive_root_validation(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "r").mkdir()
    assert wc.check_archive_root(None, []) == "archive_root_unset"
    assert wc.check_archive_root("rel", []) == "archive_root_invalid"
    root = str(tmp_path / "r")
    inside = tmp_path / "r" / "arch"
    inside.mkdir()
    assert wc.check_archive_root(str(inside), [root]) == "archive_root_invalid"
    assert wc.check_archive_root(str(tmp_path / "a"), [root]) is None


# -- references and revalidation -------------------------------------------------------


async def test_claimed_task_and_active_agent_block(env: Env) -> None:
    path = await env.instance(A)
    await insert_agent(env.db, "ag-1", "active")
    async with env.db.write() as tx:
        await tx.conn.execute(
            "UPDATE agents SET instance_id = ? WHERE id = 'ag-1'", (A,)
        )
    await env.run()
    assert path.exists()
    assert (await env.entry(A))["reason"] == "agent_active"

    async with env.db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        claimed = await tasks.claim(tx, "ag-1", [])
        assert claimed is not None
        await tx.conn.execute(
            "UPDATE agents SET state = 'gone', ended_at = ? WHERE id = 'ag-1'",
            (ts(tx.now),),
        )
    await env.run()
    assert path.exists()
    assert (await env.entry(A))["reason"] == "task_claimed"


@pytest.mark.parametrize("field", ["install_dir", "instances_root"])
async def test_shared_pool_paths_block(env: Env, field: str) -> None:
    """Another pool's install dir inside the root refuses the whole root; another
    pool's instances root inside an instance directory blocks that directory."""
    path = await env.instance(A)
    (path / "shared").mkdir()
    real = service.list_pools

    async def with_shared(conn, *args, **kwargs):
        pools = await real(conn, *args, **kwargs)
        n = pools[0].nanoclaw
        assert n is not None
        shared = n.model_copy(update={field: str(path / "shared")})
        return [
            *pools,
            pools[0].model_copy(update={"name": "other", "nanoclaw": shared}),
        ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "list_pools", with_shared)
        run = await env.run()
    assert (path / "notes.txt").exists()
    if field == "install_dir":
        assert any("overlaps_protected_path" in x for x in run.skipped)
    else:
        assert (await env.entry(A))["reason"] == "shared_path"


@pytest.mark.parametrize(
    ("running", "inside", "reason"),
    [(False, True, "container_mount"), (True, False, "container_mount")],
)
async def test_container_mounts_block(
    env: Env, running: bool, inside: bool, reason: str
) -> None:
    path = await env.instance(A)
    source = path / "sub" if inside else env.root
    env.target.mounts = [("c1", str(source), running)]
    await env.run()
    assert path.exists()
    assert (await env.entry(A))["reason"] == reason


async def test_container_check_failure_fails_closed(env: Env) -> None:
    path = await env.instance(A)
    env.target.mounts_error = True
    await env.run()
    assert path.exists()
    assert (await env.entry(A))["reason"] == "container_check_unavailable"


async def test_process_reference_blocks(env: Env) -> None:
    path = await env.instance(A)
    (env.proc / "4242").mkdir()
    (env.proc / "4242" / "cwd").symlink_to(path)
    await env.run()
    assert path.exists()
    assert (await env.entry(A))["reason"] == "process_reference"


async def test_state_flip_after_lease_aborts_and_restores(env: Env) -> None:
    path = await env.instance(A)
    cleaner = env.cleaner()
    original = cleaner._revalidate

    async def flip(cand, paths):
        async with env.db.write() as tx:
            await tx.conn.execute(
                "UPDATE instances SET state = 'running', terminated_at = NULL"
                " WHERE id = ?",
                (A,),
            )
        return await original(cand, paths)

    cleaner._revalidate = flip  # type: ignore[method-assign]
    await cleaner.run("manual", "admin:test", False)
    assert (path / "notes.txt").exists()
    assert not (env.root / (wc.TOMBSTONE + A)).exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "state_changed")


async def test_state_flip_before_lease_aborts(env: Env) -> None:
    path = await env.instance(A)
    cleaner = env.cleaner()
    original = cleaner._lease

    async def flip(run, cand):
        async with env.db.write() as tx:
            await tx.conn.execute(
                "UPDATE instances SET state = 'reserved', terminated_at = NULL"
                " WHERE id = ?",
                (A,),
            )
        return await original(run, cand)

    cleaner._lease = flip  # type: ignore[method-assign]
    await cleaner.run("manual", "admin:test", False)
    assert path.exists()
    assert (await env.entry(A))["reason"] == "state_changed"


async def test_concurrent_cleaners_delete_once(env: Env) -> None:
    await env.instance(A)
    await env.instance(B)
    runs = await asyncio.gather(env.run(), env.run())
    assert sum(r.cleaned for r in runs) == 2
    assert sum(r.failed for r in runs) == 0
    assert not any(env.root.iterdir())
    async with (
        env.db.read() as conn,
        conn.execute(
            "SELECT count(*) AS n FROM events WHERE kind = 'workspace.cleaned'"
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None and row["n"] == 2


async def test_a_tombstone_leased_after_the_scan_snapshot_is_left_alone(
    env: Env, monkeypatch
) -> None:
    # Another run leases and renames B after this run read its audit snapshot;
    # settling must see that lease, not the stale snapshot.
    await env.instance(A)
    await env.instance(B)
    real = wc._list_dir
    raced = False

    def list_dir(root: str) -> list[tuple[str, bool]]:
        nonlocal raced
        if not raced:
            raced = True
            os.rename(env.root / B, env.root / (wc.TOMBSTONE + B))
            asyncio.run_coroutine_threadsafe(lease_b(), loop).result()
        return real(root)

    async def lease_b() -> None:
        async with env.db.write() as tx:
            await wc._put(
                tx,
                "other-run",
                str(env.root),
                B,
                status=wc.S.ELIGIBLE,
                reason="in_progress",
                lock_run_id="other-run",
                lock_expires_at=ts(tx.now.replace(year=2099)),
            )

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(wc, "_list_dir", list_dir)
    await env.run()
    assert (env.root / (wc.TOMBSTONE + B)).exists()  # not renamed back
    assert not (env.root / B).exists()


async def test_leftover_tombstone_is_restored_unless_leased(env: Env) -> None:
    await env.instance(A)
    await env.instance(B, "running")
    os.rename(env.root / A, env.root / (wc.TOMBSTONE + A))
    os.rename(env.root / B, env.root / (wc.TOMBSTONE + B))
    async with env.db.write() as tx:
        await wc._put(
            tx,
            "other-run",
            str(env.root),
            B,
            status=wc.S.ELIGIBLE,
            reason="in_progress",
            lock_run_id="other-run",
            lock_expires_at=ts(tx.now.replace(year=2099)),
        )
    await env.run()
    assert not (env.root / A).exists()  # restored, then cleaned
    assert (env.root / (wc.TOMBSTONE + B)).exists()  # another run holds it


async def test_internal_error_keeps_the_directory(env: Env, monkeypatch) -> None:
    path = await env.instance(A)

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(wa, "scan", boom)
    run = await env.run()
    assert run.status == "finished"
    assert (path / "notes.txt").exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "internal_error")
    assert SECRET not in json.dumps(row)


# -- WIP preservation -----------------------------------------------------------------


async def test_wip_is_archived_and_restores_exactly(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    path = await env.instance(A)
    repo = make_wip_repo(env.tmp, path)
    expected = {
        "log": git(repo, "log", "--format=%H %s"),
        "staged": git(repo, "diff", "--cached", "--binary"),
        "unstaged": git(repo, "diff", "--binary"),
        "a": (repo / "a.txt").read_text(),
        "bin": (repo / "new.bin").read_bytes(),
    }
    run = await env.run()
    assert run.cleaned == 1
    assert not path.exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("cleaned", "archived")
    archive = Path(row["archive_path"])
    assert archive.parent.parent == env.archive

    manifest = wa.verify(archive)
    (top,) = manifest["repos"]
    assert top["rel"] == "repo"
    assert top["skipped_sensitive"] == [".env"]
    assert top["local_commits"] == 1
    for f in archive.iterdir():
        assert SECRET.encode() not in f.read_bytes()
    assert SECRET not in caplog.text

    dest = env.tmp / "restored"
    assert restore_main([str(archive), str(dest)]) == 0
    back = dest / "repo"
    assert git(back, "log", "--format=%H %s") == expected["log"]
    assert git(back, "diff", "--cached", "--binary") == expected["staged"]
    assert git(back, "diff", "--binary") == expected["unstaged"]
    assert (back / "a.txt").read_text() == expected["a"]
    assert (back / "new.bin").read_bytes() == expected["bin"]
    assert not (back / ".env").exists()


async def test_sensitive_only_changes_still_get_a_manifest(env: Env) -> None:
    path = await env.instance(A)
    repo = path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".env").write_text(SECRET)
    await env.run()
    row = await env.entry(A)
    assert row["reason"] == "archived"
    manifest = wa.verify(Path(row["archive_path"]))
    assert manifest["repos"][0]["skipped_sensitive"] == [".env"]


def test_repository_under_a_cache_name_outside_a_repo_is_found(tmp_path: Path) -> None:
    (tmp_path / "build" / "proj").mkdir(parents=True)
    git(tmp_path / "build" / "proj", "init", "-q")
    (tmp_path / "top").mkdir()
    git(tmp_path / "top", "init", "-q")
    (tmp_path / "top" / "node_modules" / "x").mkdir(parents=True)
    repos, caches = wa.find_repos(tmp_path)
    assert repos == [("build/proj", "worktree"), ("top", "worktree")]
    assert caches == ["top/node_modules"]


class FailingWriter(wa.ArchiveWriter):
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def open(self, path: Path) -> BinaryIO:
        if self.mode == "enospc":
            raise OSError(errno.ENOSPC, "full")
        if self.mode == "eacces":
            raise PermissionError(errno.EACCES, "denied")
        if self.mode == "boom":
            raise RuntimeError("unexpected")
        return super().open(path)

    def hash_file(self, path: Path) -> str:
        digest = super().hash_file(path)
        if self.mode == "hash" and path.name.endswith(".bundle"):
            self.mode = "hashed"  # the first read records, the re-read differs
        elif self.mode == "hashed" and path.name.endswith(".bundle"):
            return "0" * 64
        return digest


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("enospc", "archive_no_space"),
        ("eacces", "archive_permission"),
        ("hash", "archive_hash_mismatch"),
        ("boom", "archive_error"),
    ],
)
async def test_archive_failures_block_and_keep_the_source(
    env: Env, mode: str, reason: str
) -> None:
    path = await env.instance(A)
    repo = make_wip_repo(env.tmp, path)
    await env.run(writer=FailingWriter(mode))
    assert (repo / "b.txt").exists() and (repo / ".env").exists()
    assert not (env.root / (wc.TOMBSTONE + A)).exists()
    assert list(env.archive.iterdir()) == []  # temp directory discarded
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", reason)


async def test_archive_too_large_blocks(env: Env) -> None:
    path = await env.instance(A)
    make_wip_repo(env.tmp, path)
    await env.run(workspace_cleanup_max_archive_bytes=10)
    assert path.exists()
    assert (await env.entry(A))["reason"] == "archive_too_large"


async def test_missing_git_blocks(env: Env) -> None:
    path = await env.instance(A)
    make_wip_repo(env.tmp, path)
    await env.run(git=wa.Git(binary=str(env.tmp / "no-git")))
    assert path.exists()
    assert (await env.entry(A))["reason"] == "git_missing"


async def test_git_timeout_blocks(env: Env) -> None:
    path = await env.instance(A)
    make_wip_repo(env.tmp, path)
    slow = env.tmp / "slow-git"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    await env.run(git=wa.Git(binary=str(slow), timeout_s=0.2))
    assert path.exists()
    assert (await env.entry(A))["reason"] == "archive_timeout"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
async def test_unreadable_directory_blocks(env: Env) -> None:
    path = await env.instance(A)
    locked = path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        await env.run()
        assert path.exists()
        assert (await env.entry(A))["status"] == "blocked"
    finally:
        locked.chmod(0o700)


def test_verify_rejects_tampering_and_unsafe_names(tmp_path: Path) -> None:
    (tmp_path / "x").write_bytes(b"data")
    good = wa.ArchiveWriter().hash_file(tmp_path / "x")
    for name, size, sha, code in [
        ("../x", 4, good, "unsafe_path"),
        (".hidden", 4, good, "unsafe_path"),
        ("x", 5, good, "archive_hash_mismatch"),
        ("x", 4, "0" * 64, "archive_hash_mismatch"),
        ("missing", 4, good, "archive_hash_mismatch"),
    ]:
        manifest = {
            "repos": [{"artifacts": [{"name": name, "bytes": size, "sha256": sha}]}]
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(wa.ArchiveError) as exc:
            wa.verify(tmp_path)
        assert exc.value.code == code


def test_restore_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "manifest.json").write_text(json.dumps({"repos": []}))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_text("x")
    with pytest.raises(wa.ArchiveError) as exc:
        restore(tmp_path / "a", tmp_path / "d")
    assert exc.value.code == "destination_not_empty"


# -- low disk and byte accounting -----------------------------------------------------


async def test_low_disk_prunes_only_caches_of_clean_repos(env: Env) -> None:
    clean = await env.instance(A)
    origin = env.tmp / "o.git"
    git(env.tmp, "init", "-q", "--bare", str(origin))
    repo = clean / "repo"
    git(env.tmp, "clone", "-q", str(origin), str(repo))
    (repo / "a.txt").write_text("a")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    git(repo, "push", "-q", "origin", "HEAD:main")
    (repo / ".gitignore").write_text("node_modules/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    git(repo, "push", "-q", "origin", "HEAD:main")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "big.js").write_bytes(b"x" * 100_000)

    dirty = await env.instance(B)
    (env.tmp / "o2").mkdir()
    wip = make_wip_repo(env.tmp / "o2", dirty)
    (wip / "node_modules").mkdir()
    (wip / "node_modules" / "m.js").write_text("m")

    run = await env.run(workspace_cleanup_low_disk_bytes=1 << 62)
    assert run.low_disk
    assert not (repo / "node_modules").exists()
    assert (repo / "a.txt").exists() and (clean / "notes.txt").exists()
    assert (wip / "node_modules" / "m.js").exists()
    assert list(env.archive.iterdir()) == []
    for iid in (A, B):
        assert (await env.entry(iid))["reason"] == "low_disk"


def test_hardlinks_are_counted_once(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "f").write_bytes(b"x" * 50_000)
    os.link(d / "f", d / "g")
    tree = wc.measure(str(tmp_path), "d", os.geteuid())
    expected = os.lstat(d).st_blocks * 512 + os.lstat(d / "f").st_blocks * 512
    assert tree.bytes == expected


# -- REST and MCP -------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "w" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def orchestrator(client: TestClient) -> dict[str, str]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "orchestrator"}, headers=ADMIN
    ).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_rest_summary_dry_run_and_guards(client: TestClient) -> None:
    orch = orchestrator(client)
    res = client.get("/api/fleet/workspace-cleanup", headers=orch)
    assert res.status_code == 200
    body = res.json()
    assert body["settings"]["enabled"] is False
    assert body["settings"]["dry_run"] is True

    res = client.post("/api/fleet/workspace-cleanup/run", headers=orch, json={})
    assert res.status_code == 200, res.text
    assert res.json()["last_run"]["dry_run"] is True

    res = client.post(
        "/api/fleet/workspace-cleanup/run", headers=orch, json={"dry_run": False}
    )
    assert res.status_code == 403
    res = client.post(
        "/api/fleet/workspace-cleanup/run", headers=ADMIN, json={"dry_run": False}
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "workspace_cleanup_disabled"

    page = client.get(
        "/api/fleet/workspace-cleanup/instances", headers=orch, params={"limit": 5}
    )
    assert page.status_code == 200
    assert page.json() == {"total": 0, "items": []}
    assert client.get("/api/fleet/workspace-cleanup").status_code in (401, 403)


def test_rest_real_run_needs_a_valid_archive_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_WORKSPACE_CLEANUP_ENABLED", "true")
    monkeypatch.setenv("ARGUS_WORKSPACE_CLEANUP_INTERVAL_S", "3600")
    with TestClient(app) as c:
        res = c.post(
            "/api/fleet/workspace-cleanup/run", headers=ADMIN, json={"dry_run": False}
        )
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "archive_root_unset"
        archive = tmp_path / "archive"
        archive.mkdir()
        monkeypatch.setenv("ARGUS_WORKSPACE_CLEANUP_ARCHIVE_ROOT", str(archive))
    with TestClient(app) as c:
        res = c.post(
            "/api/fleet/workspace-cleanup/run", headers=ADMIN, json={"dry_run": False}
        )
        assert res.status_code == 200, res.text
        assert res.json()["last_run"]["dry_run"] is False


def test_mcp_tools(client: TestClient) -> None:
    token = orchestrator(client)["Authorization"].removeprefix("Bearer ")
    summary = tool(client, token, "get_workspace_cleanup")
    assert summary["settings"]["enabled"] is False
    ran = tool(client, token, "run_workspace_cleanup")
    assert ran["last_run"]["dry_run"] is True
    page = tool(client, token, "list_workspace_cleanup", status="blocked")
    assert page["total"] == 0
    denied = tool(client, token, "run_workspace_cleanup", dry_run=False)
    assert "error" in denied
