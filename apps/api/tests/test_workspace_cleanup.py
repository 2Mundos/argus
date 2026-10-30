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
import stat
import subprocess
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest
from api_support import FakeClock, FakeTarget, insert_agent
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
    # Strip GIT_* so a parent git hook (pre-push sets GIT_DIR) cannot redirect
    # these fixture repos into the caller's worktree.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
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
        env=env,
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


def test_remove_tree_refuses_same_device_mount_listed_in_table(tmp_path: Path) -> None:
    """Same-device bind mounts have matching st_dev; the mounts table must stop delete."""
    tree = tmp_path / "d"
    nested = tree / "bind"
    nested.mkdir(parents=True)
    (nested / "foreign.txt").write_text("keep-me")
    (tree / "own.txt").write_text("own")
    mounts = [str(nested)]
    with pytest.raises(wc.Unsafe) as exc:
        wc.remove_tree(str(tmp_path), "d", os.lstat(tree).st_dev, mounts=lambda: mounts)
    assert exc.value.code == "mount_present"
    assert (nested / "foreign.txt").read_text() == "keep-me"
    assert (tree / "own.txt").read_text() == "own"


def test_remove_tree_rereads_mounts_during_walk(tmp_path: Path) -> None:
    """A mount that appears after the walk starts still aborts (TOCTOU vs revalidation)."""
    tree = tmp_path / "d"
    nested = tree / "later"
    nested.mkdir(parents=True)
    (nested / "secret.txt").write_text("secret")
    (tree / "early.txt").write_text("early")
    calls = {"n": 0}

    def mounts() -> list[str]:
        calls["n"] += 1
        # First directory (tree root) is clear; nested sees the new mount.
        return [str(nested)] if calls["n"] > 1 else []

    with pytest.raises(wc.Unsafe) as exc:
        wc.remove_tree(str(tmp_path), "d", os.lstat(tree).st_dev, mounts=mounts)
    assert exc.value.code == "mount_present"
    assert (nested / "secret.txt").read_text() == "secret"
    assert calls["n"] >= 2


def test_remove_tree_unreadable_mounts_during_delete_fails_closed(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "d"
    tree.mkdir()
    (tree / "notes.txt").write_text("hello")

    def broken() -> list[str]:
        raise OSError(errno.EACCES, "denied")

    with pytest.raises(wc.Unsafe) as exc:
        wc.remove_tree(str(tmp_path), "d", os.lstat(tree).st_dev, mounts=broken)
    assert exc.value.code == "mounts_unreadable"
    assert (tree / "notes.txt").read_text() == "hello"


def test_remove_tree_success_control_with_empty_mounts(tmp_path: Path) -> None:
    tree = tmp_path / "d"
    tree.mkdir()
    (tree / "notes.txt").write_text("hello")
    wc.remove_tree(str(tmp_path), "d", os.lstat(tree).st_dev, mounts=list)
    assert not tree.exists()


async def test_mount_appearing_only_at_delete_blocks_and_keeps_tree(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revalidation sees no mounts; delete-time table lists a same-device path under the tombstone."""
    path = await env.instance(A)
    bind = path / "bind"
    bind.mkdir()
    (bind / "foreign.txt").write_text("keep")
    env.mounts = []

    real_remove = wc.remove_tree

    def remove_with_late_mount(
        root: str,
        name: str,
        dev: int,
        mounts: object = None,
    ) -> None:
        tomb = Path(root) / name
        real_remove(root, name, dev, mounts=lambda: [str(tomb / "bind")])

    monkeypatch.setattr(wc, "remove_tree", remove_with_late_mount)
    await env.run()
    # Tombstone may remain after delete_error; live name must not be gone cleanly
    # without preserving bind contents somewhere under the root.
    leftovers = list(env.root.iterdir())
    assert leftovers, "directory must not vanish when a late mount blocks delete"
    found = False
    for entry in leftovers:
        candidate = entry / "bind" / "foreign.txt"
        if candidate.is_file():
            assert candidate.read_text() == "keep"
            found = True
    assert found
    row = await env.entry(A)
    assert row["status"] == "failed"
    assert row["reason"] == "delete_error"
    assert row["error"] == "mount_present"


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


async def test_internal_error_keeps_the_directory(
    env: Env, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = await env.instance(A)
    caplog.set_level(logging.DEBUG)

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(wa, "scan", boom)
    run = await env.run()
    assert run.status == "finished"
    assert (path / "notes.txt").exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "internal_error")
    assert SECRET not in json.dumps(row)
    # Unexpected exceptions must not leak payloads into logs (sanitize only).
    assert SECRET not in caplog.text


async def test_resume_after_delete_interrupted_keeps_archived_reason(env: Env) -> None:
    """A leftover tombstone whose archive already landed finishes as archived."""
    path = await env.instance(A)
    make_wip_repo(env.tmp, path)
    archive_dir = env.archive / A / "prior-run"
    archive_dir.mkdir(parents=True)
    (archive_dir / "manifest.json").write_text("{}")
    os.rename(path, env.root / (wc.TOMBSTONE + A))
    async with env.db.write() as tx:
        await wc._put(
            tx,
            "prior-run",
            str(env.root),
            A,
            instance_id=A,
            pool="nc",
            status=wc.S.FAILED,
            reason="delete_interrupted",
            archive_path=str(archive_dir),
            bytes_estimated=1234,
        )
    run = await env.run()
    assert run.cleaned == 1
    assert not (env.root / (wc.TOMBSTONE + A)).exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("cleaned", "archived")
    assert row["archive_path"] == str(archive_dir)
    async with (
        env.db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'workspace.cleaned'",
            (A,),
        ) as cur,
    ):
        event = await cur.fetchone()
    assert event is not None
    assert json.loads(event["data"])["archived"] is True


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
    assert [(r.rel, r.kind) for r in repos] == [
        ("build/proj", "worktree"),
        ("top", "worktree"),
    ]
    assert caches == ["top/node_modules"]


# -- gitfiles, unborn and broken repositories, scan limits ----------------------------

MOUNT = "/mnt/agent-test"  # stands in for CONTAINER_MOUNT in gitfiles


def snapshot(repo: Path) -> dict[str, str | bytes]:
    """Everything a restore must reproduce: history, index, worktree diff and
    the untracked file list with contents."""
    untracked = git(repo, "ls-files", "--others", "-z").split("\0")
    return {
        "log": git(repo, "log", "--all", "--format=%H %D %s"),
        "head": git(repo, "symbolic-ref", "HEAD"),
        "staged": git(repo, "diff", "--cached", "--binary"),
        "unstaged": git(repo, "diff", "--binary"),
        **{f"u:{u}": (repo / u).read_bytes() for u in untracked if u},
    }


def make_linked_worktree(
    tmp: Path, path: Path
) -> tuple[Path, Path, dict[str, str | bytes]]:
    """A main clone at `path/w/main` and a linked worktree at `path/w/wt`,
    each with a commit that exists nowhere else; the worktree also has staged,
    unstaged and untracked WIP. Its gitfile and admin `gitdir` then name MOUNT,
    as NanoClaw leaves them (`commondir` stays git's relative `../..`); returns
    the worktree snapshot taken before that."""
    origin = tmp / "wt-origin.git"
    git(tmp, "init", "-q", "--bare", str(origin))
    main = path / "w" / "main"
    git(tmp, "clone", "-q", str(origin), str(main))
    (main / "a.txt").write_text("one\n")
    git(main, "add", "a.txt")
    git(main, "commit", "-q", "-m", "pushed")
    git(main, "push", "-q", "origin", "HEAD:main")
    (main / "m.txt").write_text("main only\n")
    git(main, "add", "m.txt")
    git(main, "commit", "-q", "-m", "main local")
    wt = path / "w" / "wt"
    git(main, "worktree", "add", "-q", "-b", "feature", str(wt))
    (wt / "f.txt").write_text("feature\n")
    git(wt, "add", "f.txt")
    git(wt, "commit", "-q", "-m", "feature local")
    (wt / "staged.txt").write_text("staged\n")
    git(wt, "add", "staged.txt")
    (wt / "a.txt").write_text("one\nunstaged\n")
    (wt / "new.bin").write_bytes(bytes(range(256)))
    expected = snapshot(wt) | {"main": git(main, "log", "--all", "--format=%H %s")}
    (wt / ".env").write_text(f"TOKEN={SECRET}\n")
    (wt / ".git").write_text(f"gitdir: {MOUNT}/w/main/.git/worktrees/wt\n")
    admin = main / ".git" / "worktrees" / "wt"
    (admin / "gitdir").write_text(f"{MOUNT}/w/wt/.git\n")
    assert (admin / "commondir").read_text() == "../..\n"
    return main, wt, expected


def assert_clean_audit(row: dict, tmp: Path) -> None:
    """Audit details carry reason codes only: no path, no file content."""
    for key, value in row.items():
        if key in ("root", "archive_path"):
            continue
        assert SECRET not in str(value)
        assert str(tmp) not in str(value)
    assert "/" not in (row["error"] or "")


def test_container_mount_is_the_documented_prefix() -> None:
    assert wa.CONTAINER_MOUNT == "/workspace/agent"


async def test_linked_worktree_with_container_gitdir_restores_exactly(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a `.git` file used to block the instance (`unsupported_repo`).
    The gitfile names the container mount; the instance is scanned under its
    tombstone name, so only the remapped path can work."""
    monkeypatch.setattr(wa, "CONTAINER_MOUNT", MOUNT)
    path = await env.instance(A)
    _, _, expected = make_linked_worktree(env.tmp, path)

    run = await env.run()
    row = await env.entry(A)
    assert (row["status"], row["reason"], row["error"]) == ("cleaned", "archived", None)
    assert run.cleaned == 1
    archive = Path(row["archive_path"])
    manifest = wa.verify(archive)
    by_rel = {r["rel"]: r for r in manifest["repos"]}
    assert by_rel["w/wt"]["git_dir"] == "w/main/.git/worktrees/wt"
    assert by_rel["w/wt"]["common_dir"] == "w/main/.git"
    assert by_rel["w/wt"]["skipped_sensitive"] == [".env"]
    for f in archive.iterdir():
        assert SECRET.encode() not in f.read_bytes()

    dest = env.tmp / "restored"
    restore(archive, dest)
    assert (
        git(dest / "w" / "main", "log", "--all", "--format=%H %s") == expected["main"]
    )
    back = snapshot(dest / "w" / "wt")
    for key in ("head", "staged", "unstaged", "u:new.bin"):
        assert back[key] == expected[key], key
    assert [k for k in back if k.startswith("u:")] == ["u:new.bin"]
    assert "feature local" in str(back["log"]) and "main local" in str(back["log"])


def test_submodule_gitfile_is_resolved_inside_the_instance(tmp_path: Path) -> None:
    origin = tmp_path / "sub-origin"
    origin.mkdir()
    git(origin, "init", "-q")
    (origin / "s.txt").write_text("s\n")
    git(origin, "add", "s.txt")
    git(origin, "commit", "-q", "-m", "s")
    base = tmp_path / "inst"
    parent = base / "parent"
    parent.mkdir(parents=True)
    git(parent, "init", "-q")
    git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(origin), "sub")  # fmt: skip
    git(parent, "commit", "-q", "-m", "add sub")
    sub = parent / "sub"
    assert (sub / ".git").read_text().startswith("gitdir: ../.git/modules/sub")
    (sub / "wip.txt").write_text("wip\n")
    git(sub, "add", "wip.txt")
    git(sub, "commit", "-q", "-m", "sub local")
    (sub / "loose.txt").write_text("loose\n")

    result = wa.scan(base, wa.Git())
    by_rel = {r.rel: r for r in result.repos}
    assert by_rel["parent/sub"].git_dir == "parent/.git/modules/sub"
    assert by_rel["parent/sub"].local_commits == 1
    assert by_rel["parent/sub"].untracked == ["loose.txt"]
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    out = wa.archive(
        base, result, archive_root, A, "r1", "t", wa.Git(), max_bytes=1 << 30
    )
    dest = tmp_path / "restored"
    restore(out, dest)
    back = dest / "parent" / "sub"
    assert git(back, "log", "--format=%s") == git(sub, "log", "--format=%s")
    assert (back / "loose.txt").read_text() == "loose\n"


@pytest.mark.parametrize(
    ("gitfile", "detail"),
    [
        ("gitdir: /etc", "gitdir_outside"),
        ("gitdir: ../../..", "gitdir_outside"),
        ("gitdir: ../nowhere", "gitdir_missing"),
        ("gitdir: ../link/.git", "gitdir_symlink"),
        ("gitdir: ../plain.txt", "gitdir_missing"),
        ("not a gitfile", "gitfile_invalid"),
        ("gitdir: x\nmore: y", "gitfile_invalid"),
        ("gitdir: " + "x" * 5000, "gitfile_too_large"),
        (b"gitdir: \xff", "gitfile_invalid"),
    ],
)
def test_unsafe_gitfiles_fail_closed(
    tmp_path: Path, gitfile: str | bytes, detail: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    git(outside, "init", "-q")
    base = tmp_path / "inst"
    (base / "wt").mkdir(parents=True)
    (base / "link").symlink_to(outside)
    (base / "plain.txt").write_text("x")
    data = gitfile.encode() if isinstance(gitfile, str) else gitfile
    (base / "wt" / ".git").write_bytes(data)
    with pytest.raises(wa.ArchiveError) as exc:
        wa.find_repos(base)
    assert (exc.value.code, exc.value.detail) == ("unsupported_repo", detail)


def test_unsafe_commondir_fails_closed(tmp_path: Path) -> None:
    base = tmp_path / "inst"
    gd = base / "main" / ".git" / "worktrees" / "wt"
    gd.mkdir(parents=True)
    (base / "wt").mkdir()
    (base / "wt" / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
    for text, detail in [
        ("/srv/elsewhere", "commondir_outside"),
        ("../../../..", "commondir_outside"),
        ("../../gone", "commondir_missing"),
    ]:
        (gd / "commondir").write_text(text + "\n")
        with pytest.raises(wa.ArchiveError) as exc:
            wa.find_repos(base)
        assert (exc.value.code, exc.value.detail) == ("unsupported_repo", detail)
    (gd / "commondir").write_text(f"{MOUNT}/main/.git\n")
    with pytest.raises(wa.ArchiveError) as exc:
        wa.find_repos(base, MOUNT)
    assert exc.value.detail == "commondir_absolute"
    (gd / "commondir").write_text("../..\n")
    repo = {r.rel: r for r in wa.find_repos(base)[0]}["wt"]
    assert (repo.git_dir, repo.common_dir) == ("main/.git/worktrees/wt", "main/.git")


def test_symlinked_git_entry_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    git(real, "init", "-q")
    base = tmp_path / "inst"
    (base / "wt").mkdir(parents=True)
    (base / "wt" / ".git").symlink_to(real / ".git")
    with pytest.raises(wa.ArchiveError) as exc:
        wa.find_repos(base)
    assert (exc.value.code, exc.value.detail) == ("unsupported_repo", "gitfile_type")


async def test_gitdir_outside_the_instance_blocks_with_detail(env: Env) -> None:
    path = await env.instance(A)
    wt = path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {env.tmp}/elsewhere/.git\n")
    (wt / ".env").write_text(SECRET)
    await env.run()
    assert (wt / ".git").exists() and (wt / ".env").read_text() == SECRET
    assert not (env.root / (wc.TOMBSTONE + A)).exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"], row["error"]) == (
        "blocked",
        "unsupported_repo",
        "gitdir_outside",
    )
    assert_clean_audit(row, env.tmp)


def test_symlinks_are_not_followed_in_the_scan(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "repo").mkdir(parents=True)
    git(outside / "repo", "init", "-q")
    base = tmp_path / "inst"
    base.mkdir()
    (base / "link").symlink_to(outside)
    (base / "repo-link").symlink_to(outside / "repo")
    assert wa.find_repos(base) == ([], [])


async def test_unborn_repository_wip_is_archived_and_restored(env: Env) -> None:
    path = await env.instance(A)
    repo = path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "unborn-topic")  # not the restore's default
    (repo / "s.txt").write_text("staged\n")
    git(repo, "add", "s.txt")
    (repo / "s.txt").write_text("staged\nthen edited\n")
    (repo / "u.txt").write_text("untracked\n")
    expected = snapshot(repo)
    await env.run()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("cleaned", "archived")
    dest = env.tmp / "restored"
    restore(Path(row["archive_path"]), dest)
    assert snapshot(dest / "fresh") == expected


async def test_shallow_clone_with_wip_blocks(env: Env) -> None:
    """Regression: the bundle verified against the source yet could not be
    restored without it (prerequisite commits), so WIP would have been lost."""
    origin = env.tmp / "deep"
    origin.mkdir()
    git(origin, "init", "-q")
    for n in range(2):
        (origin / "f.txt").write_text(str(n))
        git(origin, "add", "f.txt")
        git(origin, "commit", "-q", "-m", str(n))
    path = await env.instance(A)
    repo = path / "shallow"
    git(env.tmp, "clone", "-q", "--depth", "1", f"file://{origin}", str(repo))
    git(repo, "remote", "remove", "origin")
    (repo / "wip.txt").write_text("wip\n")
    await env.run()
    assert (repo / "wip.txt").exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"], row["error"]) == (
        "blocked",
        "unsupported_repo",
        "shallow",
    )
    assert list(env.archive.iterdir()) == []


def test_bundle_prerequisites_are_detected(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    for n in range(2):
        (repo / "f").write_text(str(n))
        git(repo, "add", "f")
        git(repo, "commit", "-q", "-m", str(n))
    git(repo, "bundle", "create", "-q", str(tmp_path / "full"), "--all")
    git(repo, "bundle", "create", "-q", str(tmp_path / "thin"), "HEAD~1..main")
    assert not wa._bundle_has_prerequisites(tmp_path / "full")
    assert wa._bundle_has_prerequisites(tmp_path / "thin")


@pytest.mark.parametrize("damage", ["dangling_branch", "dangling_head", "bad_head"])
async def test_broken_refs_fail_closed(env: Env, damage: str) -> None:
    path = await env.instance(A)
    repo = path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "u.txt").write_text("wip\n")
    if damage == "dangling_branch":
        (repo / ".git" / "refs" / "heads" / "main").write_text("1" * 40 + "\n")
    elif damage == "dangling_head":
        (repo / ".git" / "HEAD").write_text("2" * 40 + "\n")
    else:
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/../../evil\n")
    await env.run()
    assert (repo / "u.txt").exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "git_error")
    assert_clean_audit(row, env.tmp)


async def test_broken_git_directory_never_reads_as_no_wip(env: Env) -> None:
    """A `.git` directory git cannot use must not fall through to an enclosing
    repository (which would report no WIP for these files)."""
    outer = env.tmp / "outer"
    outer.mkdir()
    git(outer, "init", "-q")
    path = await env.instance(A)
    repo = path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "u.txt").write_text("wip\n")
    await env.run()
    assert (repo / "u.txt").exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "git_error")


@pytest.mark.parametrize(
    ("limit", "value", "detail"),
    [
        ("MAX_SCAN_DEPTH", 3, "depth"),
        ("MAX_REPOS", 1, "repo_count"),
        ("MAX_SCAN_ENTRIES", 5, "entry_count"),
    ],
)
async def test_scan_limits_block_with_detail(
    env: Env, monkeypatch: pytest.MonkeyPatch, limit: str, value: int, detail: str
) -> None:
    path = await env.instance(A)
    (path / "a" / "b" / "c" / "d" / "e").mkdir(parents=True)
    for name in ("r1", "r2"):
        (path / name).mkdir()
        git(path / name, "init", "-q")
    monkeypatch.setattr(wa, limit, value)
    await env.run()
    assert path.exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"], row["error"]) == (
        "blocked",
        "scan_limit",
        detail,
    )
    assert_clean_audit(row, env.tmp)


async def test_tree_depth_limit_blocks_with_detail(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await env.instance(A)
    (path / "a" / "b" / "c" / "d" / "e").mkdir(parents=True)
    monkeypatch.setattr(wc, "MAX_TREE_DEPTH", 3)
    await env.run()
    assert path.exists()
    row = await env.entry(A)
    assert (row["reason"], row["error"]) == ("scan_limit", "tree_depth")


async def test_deep_repository_and_deep_caches_do_not_trip_the_limit(
    env: Env,
) -> None:
    """Regression: depth 16 blocked real instances; caches are never walked."""
    path = await env.instance(A)
    deep = path.joinpath(*[f"d{n}" for n in range(20)])
    deep.mkdir(parents=True)
    git(deep, "init", "-q")
    (deep / "u.txt").write_text("wip\n")
    cache = deep / "node_modules"
    cache.joinpath(*[f"n{n}" for n in range(wa.MAX_SCAN_DEPTH + 10)]).mkdir(
        parents=True
    )
    await env.run()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("cleaned", "archived")


@pytest.mark.parametrize("mode", ["enospc", "eacces", "hash", "boom"])
async def test_worktree_archive_failures_keep_the_source(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(wa, "CONTAINER_MOUNT", MOUNT)
    path = await env.instance(A)
    main, wt, _ = make_linked_worktree(env.tmp, path)
    gitfile = (wt / ".git").read_text()
    await env.run(writer=FailingWriter(mode))
    assert (wt / "f.txt").exists() and (wt / ".git").read_text() == gitfile
    assert (wt / ".env").read_text() == f"TOKEN={SECRET}\n"
    assert (main / ".git" / "worktrees" / "wt" / "index").is_file()
    assert not (env.root / (wc.TOMBSTONE + A)).exists()
    assert list(env.archive.iterdir()) == []
    row = await env.entry(A)
    assert row["status"] == "blocked"
    assert_clean_audit(row, env.tmp)


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


async def test_permission_error_blocks_and_restores_source(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EACCES during measure blocks cleanup and renames the tombstone back.

    Inject PermissionError so the assertion holds under root and non-root.
    chmod(0) alone is not enough: as root, DAC is bypassed, the directory is
    deleted, and a finally-chmod then raises FileNotFoundError.
    """
    path = await env.instance(A)
    marker = path / "keep.txt"
    marker.write_text("preserve-me")

    def deny(root: str, name: str, uid: int) -> wc.Tree:
        raise PermissionError(errno.EACCES, "Permission denied", name)

    monkeypatch.setattr(wc, "measure", deny)
    run = await env.run()
    assert run.cleaned == 0 and run.failed == 0
    assert path.exists()
    assert marker.read_text() == "preserve-me"
    assert sorted(p.name for p in env.root.iterdir()) == [A]
    assert not (env.root / (wc.TOMBSTONE + A)).exists()
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "permission_denied")


async def test_permission_error_success_control_still_cleans(env: Env) -> None:
    """Same layout without the fault: cleanup deletes the directory."""
    path = await env.instance(A)
    (path / "keep.txt").write_text("preserve-me")
    run = await env.run()
    assert run.cleaned == 1
    assert not path.exists()
    assert not any(env.root.iterdir())
    assert (await env.entry(A))["status"] == "cleaned"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode bits")
async def test_unreadable_directory_mode_zero_blocks_as_nonroot(env: Env) -> None:
    """Optional real DAC check: mode 0 on a subdirectory yields permission_denied."""
    path = await env.instance(A)
    marker = path / "keep.txt"
    marker.write_text("preserve-me")
    locked = path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        run = await env.run()
        assert run.cleaned == 0
        assert path.exists()
        assert marker.read_text() == "preserve-me"
        assert not (env.root / (wc.TOMBSTONE + A)).exists()
        assert (await env.entry(A))["status"] == "blocked"
        assert (await env.entry(A))["reason"] == "permission_denied"
    finally:
        if locked.exists():
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


# -- directory removability (spec 3.3) -----------------------------------------------


def _dir_stat(uid: int, gid: int, mode: int) -> SimpleNamespace:
    return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=0o040000 | mode)


@pytest.mark.parametrize(
    ("owner", "mode", "gid_in_groups", "expected"),
    [
        (1000, 0o500, False, True),  # owned: the delete may chmod it
        (2000, 0o755, False, False),
        (2000, 0o777, False, True),
        (2000, 0o700, False, False),
        (2000, 0o770, True, True),
        (2000, 0o750, False, False),
        (2000, 0o770, False, False),  # group bits do not apply without membership
        (2000, 0o077, True, False),  # owner bits must be rwx
    ],
)
def test_can_remove_dir_rule(
    owner: int, mode: int, gid_in_groups: bool, expected: bool
) -> None:
    st = _dir_stat(owner, 50, mode)
    assert wc._can_remove_dir(st, 1000, {50} if gid_in_groups else {7}) is expected


def test_can_remove_dir_root_always_true() -> None:
    assert wc._can_remove_dir(_dir_stat(2000, 50, 0o000), 0, set())


def test_measure_flags_foreign_dir_without_rwx(tmp_path: Path) -> None:
    (tmp_path / "inst" / "sub").mkdir(parents=True)
    (tmp_path / "inst" / "sub" / "f").write_text("x")
    (tmp_path / "inst" / "sub").chmod(0o755)
    fake = os.geteuid() + 1
    assert wc.measure(str(tmp_path), "inst", os.geteuid()).unremovable_dir is False
    # Simulate a foreign uid: every directory must then be rwx for its class.
    for d in (tmp_path / "inst", tmp_path / "inst" / "sub"):
        d.chmod(0o777)
    assert wc.measure(str(tmp_path), "inst", fake).unremovable_dir is False
    (tmp_path / "inst" / "sub").chmod(0o755)
    assert wc.measure(str(tmp_path), "inst", fake).unremovable_dir is True


async def test_nested_tree_owned_by_service_user_is_cleaned(env: Env) -> None:
    path = await env.instance(A)
    deep = path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("x")
    (path / "a" / "g.txt").write_text("y")
    (path / "a" / "b").chmod(0o555)  # owned, read-only: the delete chmods it
    run = await env.run()
    assert run.cleaned == 1
    assert not path.exists()


async def test_unremovable_dir_blocks_wip_instance_untouched(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await env.instance(A)
    repo = make_wip_repo(env.tmp, path)
    (path / "locked").mkdir()
    before = snapshot(repo)
    monkeypatch.setattr(wc, "_can_remove_dir", lambda st, uid, groups: False)
    run = await env.run()
    assert run.cleaned == 0
    row = await env.entry(A)
    assert (row["status"], row["reason"]) == ("blocked", "permission_denied")
    assert row["error"] == "dir_not_writable"
    assert snapshot(repo) == before
    assert (path / "locked").exists()
    assert not any(env.archive.iterdir())


def test_measure_ignores_empty_foreign_dir(tmp_path: Path) -> None:
    (tmp_path / "inst" / "agent").mkdir(parents=True)
    (tmp_path / "inst").chmod(0o777)
    (tmp_path / "inst" / "agent").chmod(0o755)
    fake = os.geteuid() + 1  # the stub reads as foreign and not writable
    assert wc.measure(str(tmp_path), "inst", fake).unremovable_dir is False
    (tmp_path / "inst" / "agent" / "f").write_text("x")
    assert wc.measure(str(tmp_path), "inst", fake).unremovable_dir is True


def _foreign(monkeypatch: pytest.MonkeyPatch, *dirs: Path) -> list[int]:
    """Make `dirs` read as root-owned 0755 to the removability rule, and record
    every fchmod instead of making it (the cleaner must never chmod these)."""
    inodes = {d.stat().st_ino for d in dirs}
    real = wc._can_remove_dir
    monkeypatch.setattr(
        wc,
        "_can_remove_dir",
        lambda st, uid, groups: st.st_ino not in inodes and real(st, uid, groups),
    )
    chmods: list[int] = []
    monkeypatch.setattr(wc.os, "fchmod", lambda fd, mode: chmods.append(fd))
    return chmods


async def test_empty_root_owned_mount_stubs_do_not_block(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await env.instance(A)
    stubs = [path / "sess-1" / "agent", path / "sess-1" / "group"]
    for d in stubs:
        d.mkdir(parents=True)
        d.chmod(0o555)
    chmods = _foreign(monkeypatch, *stubs)
    run = await env.run()
    assert run.cleaned == 1
    assert not path.exists()
    assert chmods == []


async def test_root_owned_session_dir_blocks_without_chmod_and_retries_bounded(
    env: Env, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await env.instance(A)
    group = path / "sess-1" / "group"
    group.mkdir(parents=True)
    (group / "CLAUDE.md").write_text("x")
    group.chmod(0o755)
    chmods = _foreign(monkeypatch, group)
    cleaner = env.cleaner()
    for _ in range(3):  # max_attempts
        await cleaner.run("loop", wc.ACTOR, False)
    row = await env.entry(A)
    assert (row["status"], row["reason"], row["error"]) == (
        "blocked",
        "permission_denied",
        "dir_not_writable",
    )
    assert row["attempts"] == 3
    for _ in range(5):  # capped: no attempt until the backoff elapsed
        await cleaner.run("loop", wc.ACTOR, False)
    assert (await env.entry(A))["attempts"] == 3
    clock.advance(3601)
    await cleaner.run("loop", wc.ACTOR, False)
    assert (await env.entry(A))["attempts"] == 4
    assert stat.S_IMODE(group.stat().st_mode) == 0o755
    assert (group / "CLAUDE.md").read_text() == "x"
    assert chmods == []
    assert not any(env.archive.iterdir())


async def test_capped_dir_backs_off_exponentially_and_never_spins(
    env: Env, clock: FakeClock
) -> None:
    path = await env.instance(A)
    (env.proc / "4242").mkdir()
    (env.proc / "4242" / "cwd").symlink_to(path)
    cleaner = env.cleaner()
    for _ in range(3):
        await env.run()
    assert (await env.entry(A))["attempts"] == 3

    async def attempts_after(seconds: float) -> int:
        clock.advance(seconds)
        run = await cleaner.run("loop", wc.ACTOR, False)
        assert run.blocked == 1
        row = await env.entry(A)
        assert (row["status"], row["reason"]) == ("blocked", "process_reference")
        return row["attempts"]

    assert await attempts_after(3599) == 3  # backoff 1 interval
    assert await attempts_after(2) == 4
    assert await attempts_after(3601) == 4  # backoff now 2 intervals
    assert await attempts_after(3600) == 5
    async with db_write(env) as tx:
        await tx.conn.execute("UPDATE workspace_cleanup SET attempts = 100")
    assert await attempts_after(wc.MAX_RETRY_BACKOFF_S - 1) == 100  # capped
    assert await attempts_after(1) == 101
    assert path.exists()


def db_write(env: Env):
    return env.db.write()


async def test_capped_dir_retries_at_once_when_its_blocker_clears(env: Env) -> None:
    path = await env.instance(A)
    (env.proc / "4242").mkdir()
    (env.proc / "4242" / "cwd").symlink_to(path)
    for _ in range(3):
        await env.run()
    cleaner = env.cleaner()
    await cleaner.run("loop", wc.ACTOR, False)
    assert (await env.entry(A))["attempts"] == 3
    (env.proc / "4242" / "cwd").unlink()
    (env.proc / "4242").rmdir()
    run = await cleaner.run("loop", wc.ACTOR, False)  # no clock change
    assert run.cleaned == 1
    assert not path.exists()


async def test_notify_is_noop_when_disabled(env: Env) -> None:
    cleaner = env.cleaner(workspace_cleanup_enabled=False)
    cleaner.notify_terminated(A)
    assert not cleaner._terminated.is_set()
    enabled = env.cleaner()
    enabled.notify_terminated(A)
    assert enabled._terminated.is_set()


async def test_notifications_coalesce_into_one_follow_up_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.instance(A)
    cleaner = env.cleaner()
    triggers: list[str] = []
    entered, gate = asyncio.Event(), asyncio.Event()
    real = cleaner._run

    async def gated(trigger: str, actor: str, dry_run: bool):
        triggers.append(trigger)
        entered.set()
        await gate.wait()
        gate.clear()
        return await real(trigger, actor, dry_run)

    monkeypatch.setattr(cleaner, "_run", gated)

    async def release() -> None:
        entered.clear()
        gate.set()

    follower = asyncio.create_task(cleaner.follow_terminations())
    try:
        manual = asyncio.create_task(cleaner.run("manual", "admin:test", False))
        await entered.wait()
        for _ in range(3):  # while a run is busy
            cleaner.notify_terminated(A)
        await asyncio.sleep(0.01)
        assert triggers == ["manual"]
        await release()
        await manual
        await entered.wait()
        assert triggers == ["manual", "terminated"]
        for _ in range(3):  # during the triggered run
            cleaner.notify_terminated(B)
        await release()
        await entered.wait()
        assert triggers == ["manual", "terminated", "terminated"]
        await release()
        await asyncio.sleep(0.05)
        assert triggers == ["manual", "terminated", "terminated"]
        assert not cleaner.busy
    finally:
        follower.cancel()
    async with (
        env.db.read() as conn,
        conn.execute(
            "SELECT trigger, status FROM workspace_cleanup_runs ORDER BY rowid"
        ) as cur,
    ):
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [
        ("manual", "finished"),
        ("terminated", "finished"),
        ("terminated", "finished"),
    ]


async def _terminate_via_reconciler(env: Env, cleaner: wc.WorkspaceCleaner) -> None:
    from argus_api.fleet.reconciler import FleetSettings, reconcile_pool

    # The instance's container is gone from the listing: the reconciler
    # confirms it terminated, commits, then notifies the cleaner.
    await reconcile_pool(
        env.db,
        "nc",
        env.registry,
        FleetSettings(argus_url="http://x"),
        cleaner.notify_terminated,
    )


async def _settle(cleaner: wc.WorkspaceCleaner) -> None:
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not cleaner._terminated.is_set() and not cleaner.busy:
            return


async def test_reconciler_termination_triggers_safe_cleanup(env: Env) -> None:
    path = await env.instance(A, state="running")
    cleaner = env.cleaner()
    follower = asyncio.create_task(cleaner.follow_terminations())
    try:
        await cleaner.run("manual", "admin:test", False)  # running: never touched
        assert path.exists()
        await _terminate_via_reconciler(env, cleaner)
        await _settle(cleaner)
    finally:
        follower.cancel()
    assert not path.exists()
    row = await env.entry(A)
    assert row["status"] == "cleaned"


async def test_triggered_run_keeps_dir_mounted_by_container(env: Env) -> None:
    path = await env.instance(A, state="running")
    env.target.mounts = [("agent-1", str(path / "sess-1"), True)]  # still alive
    cleaner = env.cleaner()
    follower = asyncio.create_task(cleaner.follow_terminations())
    try:
        await _terminate_via_reconciler(env, cleaner)
        await _settle(cleaner)
    finally:
        follower.cancel()
    assert (path / "notes.txt").read_text() == "hello"
    assert (await env.entry(A))["reason"] == "container_mount"


async def test_triggered_run_respects_min_age_and_loop_falls_back(
    env: Env, clock: FakeClock
) -> None:
    path = await env.instance(A, state="running")
    cleaner = env.cleaner(
        workspace_cleanup_min_age_hours=1, workspace_cleanup_interval_s=0.01
    )
    follower = asyncio.create_task(cleaner.follow_terminations())
    try:
        await _terminate_via_reconciler(env, cleaner)
        await _settle(cleaner)
    finally:
        follower.cancel()
    assert path.exists()
    assert (await env.entry(A))["reason"] == "too_recent"
    clock.advance(3601)
    loop = asyncio.create_task(cleaner.loop())
    triggers: list[str] = []
    try:
        for _ in range(300):
            await asyncio.sleep(0.01)
            async with (
                env.db.read() as conn,
                conn.execute(
                    "SELECT trigger FROM workspace_cleanup_runs WHERE cleaned = 1"
                ) as cur,
            ):
                triggers = [r["trigger"] for r in await cur.fetchall()]
            if triggers:
                break
    finally:
        loop.cancel()
    assert not path.exists()
    assert triggers == ["loop"]
