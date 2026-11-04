"""The real-runtime disk guard harness without a runtime: prerequisites, the
isolation preflight, teardown ownership and evidence (disk_guard_runtime.py).

Nothing here mounts, starts a daemon or needs privilege; host views, commands
and the Docker client are fakes.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import disk_guard_runtime
import pytest
from api_support import FakeTarget
from argus_api.db import Database
from disk_guard_runtime import (
    BASE_PREFIX,
    FS_MIB,
    MARKER_FILE,
    MARKER_LABEL,
    MIB,
    MKDTEMP_RANDOM_CHARS,
    OPT_IN,
    TMPDIR_ENV,
    TOOLS,
    Evidence,
    Harness,
    HostProbe,
    Layout,
    MountEntry,
    Refused,
    bounded_fs_failures,
    containerd_argv,
    dockerd_argv,
    gate,
    has_capability,
    isolation_failures,
    missing_prerequisites,
    mounts_under,
    parse_mountinfo,
    seed_world,
)

TOKEN = "tok123"
CAP_SYS_ADMIN_ONLY = "CapEff:\t0000000000200000\n"


def probe(**kw: Any) -> HostProbe:
    files = {
        "/proc/self/status": "CapEff:\t0000000000000000\n",
        "/sys/fs/cgroup/cgroup.controllers": "cpuset cpu io memory pids\n",
        "/proc/self/mountinfo": "30 1 0:26 / /sys/fs/cgroup rw,nosuid - cgroup2 cgroup2 rw\n",
        **kw.pop("files", {}),
    }
    tools = kw.pop("tools", set(TOOLS))
    return HostProbe(
        which=lambda t: f"/usr/bin/{t}" if t in tools else None,
        euid=lambda: kw.get("euid", 0),
        read=lambda p: files.get(p),
        exists=lambda p: kw.get("loop", True),
    )


# -- prerequisites ------------------------------------------------------------------


def test_a_capable_host_misses_nothing() -> None:
    assert missing_prerequisites(probe()) == []
    assert gate({OPT_IN: "1"}, probe()) == ("run", [])


@pytest.mark.parametrize(
    ("kw", "missing"),
    [
        ({"tools": set(TOOLS) - {"dockerd"}}, "`dockerd` on PATH"),
        ({"euid": 1000}, "root or CAP_SYS_ADMIN"),
        ({"files": {"/sys/fs/cgroup/cgroup.controllers": None}}, "cgroup v2"),
        (
            {"files": {"/sys/fs/cgroup/cgroup.controllers": "cpu memory"}},
            "io controller",
        ),
        (
            {
                "files": {
                    "/proc/self/mountinfo": "30 1 0:26 / /sys/fs/cgroup ro - cgroup2 c ro\n"
                }
            },
            "writable /sys/fs/cgroup",
        ),
        ({"loop": False}, "loop device"),
    ],
)
def test_each_missing_prerequisite_is_named(kw: dict, missing: str) -> None:
    found = missing_prerequisites(probe(**kw))
    assert len(found) == 1 and missing in found[0], found
    # Opted in, a missing prerequisite fails; it never turns into a skip.
    assert gate({OPT_IN: "1"}, probe(**kw)) == ("fail", found)


def test_no_tools_names_every_tool() -> None:
    found = missing_prerequisites(probe(tools=set()))
    assert [f for f in found if "on PATH" in f] == [f"`{t}` on PATH" for t in TOOLS]


def test_cap_sys_admin_without_root_is_enough() -> None:
    p = probe(euid=1000, files={"/proc/self/status": CAP_SYS_ADMIN_ONLY})
    assert missing_prerequisites(p) == []
    assert not has_capability("CapEff:\t0000000000000000", 21)
    assert not has_capability("", 21)


def test_without_opt_in_it_skips_before_probing() -> None:
    def boom(_: str) -> None:
        raise AssertionError("probed the host without the opt-in")

    verdict, why = gate({OPT_IN: "0"}, HostProbe(which=boom))
    assert verdict == "skip" and OPT_IN in why[0]


def test_mountinfo_parses_escapes_and_skips_garbage() -> None:
    text = (
        "36 35 98:0 / /mnt/a\\040b rw,noatime master:1 - ext4 /dev/loop0 rw\n"
        "garbage\n"
        "37 35 0:5 / /tmp/x rw - tmpfs none rw\n"
    )
    assert parse_mountinfo(text) == [
        MountEntry("/mnt/a b", "ext4", "/dev/loop0", frozenset({"rw", "noatime"})),
        MountEntry("/tmp/x", "tmpfs", "none", frozenset({"rw"})),
    ]


# -- isolation preflight ----------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path / "h")


def good_info(layout: Layout) -> dict[str, Any]:
    return {
        "DockerRootDir": str(layout.data_root),
        "Labels": [f"{MARKER_LABEL}={TOKEN}"],
        "ID": "ENGINE",
        "Containers": 0,
    }


def preflight(layout: Layout, **kw: Any) -> list[str]:
    args: dict[str, Any] = {
        "spawned_pid": 42,
        "pidfile_pid": 42,
        "info": good_info(layout),
        "engine_id": "ENGINE",
        "container_ids": [],
        "outer_docker_host": None,
    }
    args.update(kw)
    return isolation_failures(layout, TOKEN, **args)


def test_the_harness_daemon_passes(layout: Layout) -> None:
    assert preflight(layout) == []
    # DOCKER_HOST pointing elsewhere is fine: the harness never uses it.
    assert preflight(layout, outer_docker_host="unix:///var/run/docker.sock") == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"spawned_pid": None}, "pidfile"),
        ({"pidfile_pid": 7}, "pidfile"),
        ({"engine_id": None}, "daemon ID"),
        ({"engine_id": "OTHER"}, "daemon ID"),
        ({"container_ids": ["abc"]}, "already has containers"),
    ],
)
def test_preflight_refuses_unproven_ownership(
    layout: Layout, change: dict, reason: str
) -> None:
    fails = preflight(layout, **change)
    assert len(fails) == 1 and reason in fails[0], fails


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        ({"DockerRootDir": "/var/lib/docker"}, "DockerRootDir"),
        ({"DockerRootDir": None}, "DockerRootDir"),
        ({"Labels": [f"{MARKER_LABEL}=someone-else"]}, "ownership label"),
        ({"Labels": None}, "ownership label"),
        ({"ID": "OTHER"}, "daemon ID"),
        ({"Containers": 3}, "already has containers"),
    ],
)
def test_preflight_refuses_a_daemon_that_is_not_ours(
    layout: Layout, info: dict, reason: str
) -> None:
    fails = preflight(layout, info=good_info(layout) | info)
    assert len(fails) == 1 and reason in fails[0], fails


def test_preflight_refuses_the_host_or_outer_socket(tmp_path: Path) -> None:
    # A base symlinked to /var/run makes the socket the host's own.
    (tmp_path / "h").symlink_to("/var/run", target_is_directory=True)
    fails = preflight(Layout(tmp_path / "h"))
    assert any("host's default" in f for f in fails), fails
    inside = Layout(tmp_path / "i")
    fails = preflight(inside, outer_docker_host=f"unix://{inside.socket}")
    assert any("DOCKER_HOST" in f for f in fails), fails


def test_teardown_identity_check_ignores_its_own_containers(layout: Layout) -> None:
    assert preflight(layout, container_ids=["abc"], fresh=False) == []


def test_daemon_and_containerd_paths_stay_under_the_harness(layout: Layout) -> None:
    for argv in (dockerd_argv(layout, TOKEN), containerd_argv(layout)):
        paths = [a.removeprefix("unix://") for a in argv if "/" in a]
        assert paths and all(p.startswith(str(layout.base)) for p in paths), argv
    argv = dockerd_argv(layout, TOKEN)
    assert f"{MARKER_LABEL}={TOKEN}" in argv and "--iptables=false" in argv


def test_bounded_fs_must_be_a_small_loop_on_the_harness_tmpfs(layout: Layout) -> None:
    good = [
        MountEntry(str(layout.backing), "tmpfs", "argus-dg", frozenset()),
        MountEntry(str(layout.fs), "ext4", "/dev/loop3", frozenset()),
    ]
    assert bounded_fs_failures(layout, good, FS_MIB * MIB) == []
    assert "above" in bounded_fs_failures(layout, good, FS_MIB * MIB + 1)[0]
    host_disk = [good[0], MountEntry(str(layout.fs), "ext4", "/dev/sda1", frozenset())]
    assert "loop" in bounded_fs_failures(layout, host_disk, MIB)[0]
    assert "tmpfs" in bounded_fs_failures(layout, good[1:], MIB)[0]


def test_mounts_under_is_deepest_first_and_skips_siblings() -> None:
    base = Path("/t/h")
    mounts = [
        MountEntry(p, "x", "x", frozenset())
        for p in ("/t/h/backing", "/t/h/fs", "/t/h/fs/docker/overlay2/m", "/t/hx", "/t")
    ]
    assert mounts_under(base, mounts) == [
        "/t/h/fs/docker/overlay2/m",
        "/t/h/backing",
        "/t/h/fs",
    ]


# -- the harness with fakes -------------------------------------------------------


class FakeContainers:
    def __init__(self, items: list[Any]) -> None:
        self.items = items
        self.filters: list[Any] = []

    def list(self, all: bool = False, filters: dict | None = None) -> list[Any]:
        self.filters.append(filters)
        if filters:
            want = filters["label"]
            return [c for c in self.items if want in c.labels]
        return self.items


class FakeClient:
    def __init__(self, info: dict[str, Any], containers: list[Any]) -> None:
        self._info = info
        self.containers = FakeContainers(containers)
        self.closed = False

    def info(self) -> dict[str, Any]:
        return self._info

    def close(self) -> None:
        self.closed = True


def container(cid: str, *labels: str) -> Any:
    c = SimpleNamespace(id=cid, labels=list(labels), removed=False)
    c.remove = lambda force: setattr(c, "removed", True)
    return c


class Host:
    """Records commands; `mounts` is what /proc/self/mountinfo would list
    (`kinds` gives a point's fstype and source, default tmpfs). A command fails
    when its joined argv starts with an entry of `fail`."""

    def __init__(self) -> None:
        self.ran: list[list[str]] = []
        self.mounts: list[str] = []
        self.kinds: dict[str, tuple[str, str]] = {}
        self.fail: set[str] = set()
        self.loops = ""

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.ran.append(argv)
        if any(" ".join(argv).startswith(f) for f in self.fail):
            raise subprocess.CalledProcessError(1, argv)
        if argv[0] == "umount":
            self.mounts.remove(argv[1])
        elif argv[0] == "mount":
            self.mounts.append(argv[-1])
            if "loop" in argv:
                self.kinds[argv[-1]] = ("ext4", "/dev/loop7")
                self.loops += f"/dev/loop7: []: ({argv[-2]})\n"
        elif argv[:2] == ["losetup", "-d"]:
            self.loops = "".join(
                ln for ln in self.loops.splitlines(True) if ln.split(":")[0] != argv[2]
            )
        out = self.loops if argv[:2] == ["losetup", "-j"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    def mountinfo(self) -> str:
        return "".join(
            "1 1 0:1 / {} rw - {} {} rw\n".format(p, *self.kinds.get(p, ("tmpfs", "x")))
            for p in self.mounts
        )

    def touched(self, path: str) -> list[list[str]]:
        return [argv for argv in self.ran if path in argv]


class Proc:
    pid = 42

    def __init__(self) -> None:
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return 0 if self.signals else None

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def wait(self, timeout: float) -> int:
        return 0


def owned(
    tmp_path: Path, host: Host, client: FakeClient | None, *, mounted: bool = True
) -> Harness:
    """A marked harness directory; `mounted` adds a running setup's daemons,
    mounts and loop device, otherwise it is as `Harness.create` left it."""
    lay = Layout(tmp_path / "h")
    lay.base.mkdir()
    (lay.base / MARKER_FILE).write_text(TOKEN)
    h = Harness(lay, TOKEN, run=host.run, mountinfo=host.mountinfo)
    if not mounted:
        return h
    lay.data_root.mkdir(parents=True)
    lay.backing.mkdir()
    lay.pidfile.write_text("42\n")
    (lay.data_root / "engine-id").write_text("ENGINE\n")
    lay.image.write_bytes(b"")
    h.client, h.dockerd, h.containerd = client, Proc(), Proc()
    host.mounts = [str(lay.backing), str(lay.fs), str(lay.data_root / "overlay2/m")]
    host.kinds[str(lay.fs)] = ("ext4", "/dev/loop7")
    host.loops = f"/dev/loop7: []: ({lay.image})\n"
    return h


def test_preflight_refuses_and_teardown_spares_foreign_containers(
    tmp_path: Path,
) -> None:
    host = Host()
    stranger = container("s" * 12)
    client = FakeClient({}, [stranger])
    h = owned(tmp_path, host, client)
    client._info = good_info(h.layout)
    with pytest.raises(Refused, match="already has containers"):
        h.preflight()
    assert not h.verified
    problems = h.teardown()
    assert not stranger.removed and client.containers.filters == [None]
    assert any("not re-verified" in p for p in problems)
    assert client.closed


def test_teardown_removes_only_what_it_made(tmp_path: Path) -> None:
    host = Host()
    ours = container("o" * 12, f"{MARKER_LABEL}={TOKEN}")
    client = FakeClient({}, [])
    h = owned(tmp_path, host, client)
    client._info = good_info(h.layout)
    assert h.preflight()["docker_root"] == str(h.layout.data_root)
    assert h.verified
    client.containers.items.append(ours)
    other = container("x" * 12, f"{MARKER_LABEL}=other")
    client.containers.items.append(other)
    base = h.layout.base
    assert h.teardown() == []
    assert ours.removed and not other.removed
    assert h.dockerd.signals and h.containerd.signals
    assert host.ran == [
        ["umount", str(base / "fs/docker/overlay2/m")],
        ["umount", str(base / "fs")],
        ["losetup", "-j", str(h.layout.image)],
        ["losetup", "-d", "/dev/loop7"],
        ["umount", str(base / "backing")],
    ]
    assert not base.exists()


def test_teardown_keeps_the_directory_when_a_mount_stays(tmp_path: Path) -> None:
    host = Host()
    h = owned(tmp_path, host, None)
    host.fail = {"umount"}
    problems = h.teardown()
    assert any("unmount" in p for p in problems)
    assert any("directory kept" in p for p in problems)
    assert h.layout.base.exists()


@pytest.mark.parametrize("marker", [None, "someone else"])
def test_teardown_keeps_a_directory_without_its_marker(
    tmp_path: Path, marker: str | None
) -> None:
    host = Host()
    h = owned(tmp_path, host, None)
    if marker is None:
        (h.layout.base / MARKER_FILE).unlink()
    else:
        (h.layout.base / MARKER_FILE).write_text(marker)
    problems = h.teardown()
    assert any("lacks the harness marker" in p for p in problems)
    # Ownership is checked before anything destructive: no command at all.
    assert host.ran == [] and h.layout.image.exists() and len(host.mounts) == 3


@pytest.mark.parametrize("link", ["base", "parent"])
def test_a_symlinked_layout_is_refused_and_never_torn_down(
    tmp_path: Path, link: str
) -> None:
    # The observed failure: mountinfo lists canonical paths, the Layout was the
    # lexical path through a symlinked TMPDIR.
    host = Host()
    real = owned(tmp_path, host, None)
    if link == "base":
        lexical = tmp_path / "alias"
        lexical.symlink_to(real.layout.base, target_is_directory=True)
    else:
        (tmp_path / "up").symlink_to(tmp_path, target_is_directory=True)
        lexical = tmp_path / "up" / "h"
    h = Harness(Layout(lexical), TOKEN, run=host.run, mountinfo=host.mountinfo)
    assert bounded_fs_failures(real.layout, real.mounts(), MIB) == []
    assert len(bounded_fs_failures(h.layout, h.mounts(), MIB)) == 2
    problems = h.teardown()
    assert any("not canonical" in p for p in problems), problems
    assert host.ran == [] and real.layout.image.exists() and len(host.mounts) == 3


def test_verify_fs_accepts_the_canonical_loop_on_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = owned(tmp_path, Host(), None)
    vfs = SimpleNamespace(f_blocks=FS_MIB, f_frsize=MIB)
    monkeypatch.setattr(os, "statvfs", lambda _: vfs)
    h.verify_fs()
    assert h.layout.data_dir.is_dir() and h.layout.instances.is_dir()


@pytest.mark.parametrize(
    ("phase", "cleanup"),
    [
        ("mount -t tmpfs", []),
        ("mkfs.ext4", [["losetup", "-j", "IMG"], ["umount", "BACKING"]]),
        (
            "mount -o loop",
            [
                ["losetup", "-j", "IMG"],
                ["losetup", "-d", "/dev/loop7"],
                ["umount", "BACKING"],
            ],
        ),
    ],
)
def test_teardown_after_a_failed_mount_phase_undoes_only_what_exists(
    tmp_path: Path, phase: str, cleanup: list[list[str]]
) -> None:
    host = Host()
    h = owned(tmp_path, host, None, mounted=False)
    lay = h.layout
    host.fail = {phase}
    if phase == "mount -o loop":  # a failed loop mount can leave the device bound
        host.loops = f"/dev/loop7: []: ({lay.image})\n"
    with pytest.raises(subprocess.CalledProcessError):
        h.mount_fs()
    done = len(host.ran)
    assert h.teardown() == []
    names = {"IMG": str(lay.image), "BACKING": str(lay.backing)}
    assert host.ran[done:] == [[names.get(a, a) for a in argv] for argv in cleanup]
    assert host.mounts == [] and host.loops == "" and not lay.base.exists()


@pytest.mark.parametrize(
    ("fail", "reported"),
    [
        ("umount", "unmount"),
        ("losetup -j", "losetup -j"),
        ("losetup -d", "detach /dev/loop7"),
    ],
)
def test_teardown_reports_failures_without_masking_the_primary(
    tmp_path: Path, fail: str, reported: str
) -> None:
    host = Host()
    h = owned(tmp_path, host, None)
    host.fail = {fail}
    problems: list[str] = []
    with pytest.raises(RuntimeError, match="primary"):
        try:
            raise RuntimeError("primary")
        finally:
            problems = h.teardown()
    assert any(p.startswith(reported) for p in problems), problems


def test_teardown_reports_a_busy_tree_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(path: Any, **_: Any) -> None:
        raise OSError(errno.EBUSY, "Device or resource busy", str(path))

    h = owned(tmp_path, Host(), None)
    monkeypatch.setattr(shutil, "rmtree", busy)
    problems = h.teardown()
    assert any("Device or resource busy" in p for p in problems), problems
    assert h.layout.base.exists()


def test_teardown_keeps_everything_when_mounts_are_unreadable(tmp_path: Path) -> None:
    host = Host()
    h = owned(tmp_path, host, None)

    def unreadable() -> str:
        raise PermissionError("mountinfo")

    h.mountinfo = unreadable
    problems = h.teardown()
    assert any("reading mountinfo" in p for p in problems), problems
    assert host.ran == [] and h.layout.base.exists()


def test_teardown_keeps_a_base_that_is_itself_a_mount(tmp_path: Path) -> None:
    host = Host()
    h = owned(tmp_path, host, None)
    host.mounts.insert(0, str(h.layout.base))
    problems = h.teardown()
    assert any("directory kept" in p for p in problems), problems
    assert host.touched(str(h.layout.base)) == [] and h.layout.base.exists()


def test_teardown_never_touches_foreign_mounts_or_loops(tmp_path: Path) -> None:
    host = Host()
    h = owned(tmp_path, host, None)
    base = str(h.layout.base)
    foreign = [base + "-x", base + "x/fs", str(tmp_path)]
    for p in foreign[:2]:
        Path(p).mkdir(parents=True)
        (Path(p) / "keep").write_text("")
    host.mounts += foreign
    host.loops += (
        "/dev/loop9: []: (/srv/other/fs.img)\n"
        f"/dev/loop8: []: ({base}-x/backing/fs.img)\n"
    )
    assert h.teardown() == []
    for p in [*foreign, "/dev/loop8", "/dev/loop9"]:
        assert host.touched(p) == [], p
    assert host.mounts == foreign
    assert [a for a in host.ran if a[:2] == ["losetup", "-d"]] == [
        ["losetup", "-d", "/dev/loop7"]
    ]
    assert all((Path(p) / "keep").exists() for p in foreign[:2])
    assert not h.layout.base.exists()


def test_teardown_leaves_containers_when_the_daemon_changed(tmp_path: Path) -> None:
    host = Host()
    ours = container("o" * 12, f"{MARKER_LABEL}={TOKEN}")
    client = FakeClient({}, [])
    h = owned(tmp_path, host, client)
    client._info = good_info(h.layout)
    h.preflight()
    client.containers.items.append(ours)
    client._info = good_info(h.layout) | {"ID": "SWAPPED"}
    problems = h.teardown()
    assert not ours.removed
    assert any("daemon ID" in p for p in problems)


# -- evidence and Argus state -----------------------------------------------------


def test_evidence_is_verified_only_when_every_check_held(tmp_path: Path) -> None:
    ev = Evidence()
    ev.finish()
    assert ev.outcome == "no_checks"
    ev = Evidence()
    ev.check("a", True)
    ev.check("b", False, {"x": 1})
    ev.finish()
    assert ev.outcome == "failed" and ev.failed() == ["b"]
    ev = Evidence(outcome="missing_prerequisites", unverified=["all"])
    ev.check("a", True)
    ev.finish()
    path = tmp_path / "e" / "ev.json"
    ev.write(path)
    data = json.loads(path.read_text())
    assert data["outcome"] == "missing_prerequisites" and data["unverified"] == ["all"]


async def test_seed_world_claims_one_task_per_descendant(db: Database) -> None:
    fake = FakeTarget()
    fake.shares_filesystem = True
    sessions = await seed_world(db, "unix:///x/docker.sock", Path("/srv/i"), 2, fake)
    assert len(sessions) == 2
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT state FROM tasks WHERE id IN (?, ?)",
            tuple(s.task_id for s in sessions),
        ) as cur,
    ):
        assert [r["state"] for r in await cur.fetchall()] == ["claimed", "claimed"]
    assert all(s.group_folder == f"argus-web-{s.instance_id[:8]}" for s in sessions)


# -- create: a canonical parent, checked before anything exists --------------------


def test_create_refuses_a_base_too_long_for_instances_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path / ("d" * 60)
    deep.mkdir()
    monkeypatch.setenv(TMPDIR_ENV, str(deep))
    with pytest.raises(ValueError, match="instances_root"):
        Harness.create(TOKEN)
    assert list(deep.iterdir()) == []


@pytest.mark.parametrize("given", ["link", "link/sub"])
def test_create_refuses_a_tmpdir_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    path = tmp_path / given
    monkeypatch.setenv(TMPDIR_ENV, str(path))
    with pytest.raises(Refused, match="canonical path") as refused:
        Harness.create(TOKEN)
    assert str(path) in str(refused.value)
    assert os.path.realpath(path) in str(refused.value)
    assert list(real.rglob("*")) == [real / "sub"]


@pytest.mark.parametrize("given", ["relative/dir", "/nonexistent/argus-dg-parent"])
def test_create_refuses_a_tmpdir_that_is_not_an_absolute_directory(
    monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    monkeypatch.setenv(TMPDIR_ENV, given)
    with pytest.raises(Refused, match="existing absolute directory"):
        Harness.create(TOKEN)


def test_create_accepts_the_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TMPDIR_ENV, str(tmp_path))
    # pytest's tmp_path is longer than the real limit allows.
    monkeypatch.setattr(disk_guard_runtime, "INSTANCES_ROOT_MAX", 4096)
    h = Harness.create(TOKEN)
    base = h.layout.base
    assert base.parent == tmp_path and (base / MARKER_FILE).read_text() == TOKEN
    # The length limit is computed from this name before the directory exists.
    assert base.name.startswith(BASE_PREFIX)
    assert len(base.name) == len(BASE_PREFIX) + MKDTEMP_RANDOM_CHARS
