"""The real-runtime disk guard harness without a runtime: prerequisites, the
isolation preflight, teardown ownership and evidence (disk_guard_runtime.py).

Nothing here mounts, starts a daemon or needs privilege; host views, commands
and the Docker client are fakes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from api_support import FakeTarget
from argus_api.db import Database
from disk_guard_runtime import (
    FS_MIB,
    MARKER_FILE,
    MARKER_LABEL,
    MIB,
    OPT_IN,
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
    """Records commands; `mounts` is what /proc/self/mountinfo would list."""

    def __init__(self) -> None:
        self.ran: list[list[str]] = []
        self.mounts: list[str] = []
        self.fail: set[str] = set()
        self.loops = ""

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.ran.append(argv)
        if argv[0] in self.fail:
            raise subprocess.CalledProcessError(1, argv)
        if argv[0] == "umount":
            self.mounts.remove(argv[1])
        out = self.loops if argv[:2] == ["losetup", "-j"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    def mountinfo(self) -> str:
        return "".join(f"1 1 0:1 / {p} rw - tmpfs x rw\n" for p in self.mounts)


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


def owned(tmp_path: Path, host: Host, client: FakeClient | None) -> Harness:
    lay = Layout(tmp_path / "h")
    lay.data_root.mkdir(parents=True)
    lay.backing.mkdir()
    (lay.base / MARKER_FILE).write_text(TOKEN)
    lay.pidfile.write_text("42\n")
    (lay.data_root / "engine-id").write_text("ENGINE\n")
    lay.image.write_bytes(b"")
    h = Harness(lay, TOKEN, run=host.run, mountinfo=host.mountinfo)
    h.client, h.dockerd, h.containerd = client, Proc(), Proc()
    host.mounts = [str(lay.backing), str(lay.fs), str(lay.data_root / "overlay2/m")]
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


def test_teardown_keeps_a_directory_without_its_marker(tmp_path: Path) -> None:
    host = Host()
    h = owned(tmp_path, host, None)
    (h.layout.base / MARKER_FILE).write_text("someone else")
    problems = h.teardown()
    assert any("lacks the harness marker" in p for p in problems)
    assert h.layout.base.exists()


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


def test_create_refuses_a_base_too_long_for_instances_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path / ("d" * 60)
    deep.mkdir()
    monkeypatch.setenv("ARGUS_DISK_GUARD_RT_TMPDIR", str(deep))
    with pytest.raises(ValueError, match="instances_root"):
        Harness.create(TOKEN)
    assert list(deep.iterdir()) == []
