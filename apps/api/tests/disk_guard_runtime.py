"""Opt-in real-runtime harness for the disk guard (operations.spec.md 3.5).

Everything destructive happens inside one directory this harness created and
marked: a tmpfs, a size-bounded ext4 loop filesystem backed by a file on that
tmpfs, and a dockerd + containerd whose sockets, data and state live there.
`isolation_failures` must come back empty before any container exists or any
byte is filled; teardown removes only what the harness made, and leaves (and
reports) anything it cannot prove is its own.

The pure checks are unit-tested in test_disk_guard_runtime.py; the run is
integration/test_disk_guard_real_runtime.py (`make test-disk-guard-runtime`).
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OPT_IN = "ARGUS_DISK_GUARD_REAL_RUNTIME"
MARKER_LABEL = "argus.disk-guard-harness"
MARKER_FILE = ".argus-disk-guard-harness"
MIB = 1024 * 1024
FS_MIB = 512
BACKING_SLACK_MIB = 16
CAP_SYS_ADMIN = 21
INSTANCES_ROOT_MAX = 56  # NanoclawOptions.instances_root
TOOLS = (
    "dockerd",
    "containerd",
    "containerd-shim-runc-v2",
    "runc",
    "mount",
    "umount",
    "mkfs.ext4",
    "losetup",
)
HOST_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")
CGROUP_ROOT = Path("/sys/fs/cgroup")
# cgroupfs driver default parent: the production `container_io_stat` looks here.
CGROUP_PARENT = CGROUP_ROOT / "docker"
START_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 30.0
# containerd plugins that would otherwise reach host-wide paths
# (/var/run/nri, /opt/containerd) or start a CRI server.
CONTAINERD_DISABLED = (
    "io.containerd.grpc.v1.cri",
    "io.containerd.cri.v1.images",
    "io.containerd.cri.v1.runtime",
    "io.containerd.nri.v1.nri",
    "io.containerd.internal.v1.opt",
)


class Refused(Exception):
    """Isolation or ownership was not proven; nothing destructive ran."""


# -- prerequisites ------------------------------------------------------------------


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


@dataclass(frozen=True)
class HostProbe:
    """Read-only views of the host, injectable for tests."""

    which: Callable[[str], str | None] = shutil.which
    euid: Callable[[], int] = os.geteuid
    read: Callable[[str], str | None] = read_text
    exists: Callable[[str], bool] = os.path.exists


def has_capability(status: str, bit: int) -> bool:
    """Whether `/proc/self/status` text grants effective capability `bit`."""
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            return bool(int(line.split()[1], 16) >> bit & 1)
    return False


@dataclass(frozen=True)
class MountEntry:
    point: str
    fstype: str
    source: str
    options: frozenset[str]


def _unescape(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \ooo.
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and field[i + 1 : i + 4].isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def parse_mountinfo(text: str) -> list[MountEntry]:
    entries = []
    for line in text.splitlines():
        head, sep, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if not sep or len(fields) < 6 or len(rest) < 2:
            continue
        entries.append(
            MountEntry(
                _unescape(fields[4]),
                rest[0],
                _unescape(rest[1]),
                frozenset(fields[5].split(",")),
            )
        )
    return entries


def missing_prerequisites(probe: HostProbe | None = None) -> list[str]:
    """Every host prerequisite the run lacks, by name; empty when it can run."""
    probe = probe or HostProbe()
    missing = [f"`{tool}` on PATH" for tool in TOOLS if probe.which(tool) is None]
    if probe.euid() != 0 and not has_capability(
        probe.read("/proc/self/status") or "", CAP_SYS_ADMIN
    ):
        missing.append("root or CAP_SYS_ADMIN (mount, loop devices, dockerd)")
    controllers = probe.read(str(CGROUP_ROOT / "cgroup.controllers"))
    if controllers is None:
        missing.append(f"cgroup v2 unified hierarchy at {CGROUP_ROOT}")
    elif "io" not in controllers.split():
        missing.append("cgroup v2 io controller (io.stat)")
    cgroup = [
        m
        for m in parse_mountinfo(probe.read("/proc/self/mountinfo") or "")
        if m.point == str(CGROUP_ROOT)
    ]
    if cgroup and "ro" in cgroup[-1].options:
        missing.append(f"writable {CGROUP_ROOT} (dockerd creates container cgroups)")
    if not probe.exists("/dev/loop-control"):
        missing.append("loop device support (/dev/loop-control)")
    return missing


def gate(env: dict[str, str], probe: HostProbe | None = None) -> tuple[str, list[str]]:
    """`("skip", [why])` without the opt-in, `("fail", missing)` when opted in on a
    host that cannot run it (never a silent skip), `("run", [])` otherwise."""
    if env.get(OPT_IN) != "1":
        return "skip", [f"opt-in: set {OPT_IN}=1 (make test-disk-guard-runtime)"]
    if missing := missing_prerequisites(probe):
        return "fail", missing
    return "run", []


# -- layout and isolation -------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Every path the harness uses, all under `base`."""

    base: Path

    @property
    def backing(self) -> Path:  # tmpfs holding the loop image
        return self.base / "backing"

    @property
    def image(self) -> Path:
        return self.backing / "fs.img"

    @property
    def fs(self) -> Path:  # the bounded ext4 filesystem the test fills
        return self.base / "fs"

    @property
    def data_root(self) -> Path:
        return self.fs / "docker"

    @property
    def containerd_root(self) -> Path:
        return self.fs / "containerd"

    @property
    def data_dir(self) -> Path:
        return self.fs / "data"

    @property
    def instances(self) -> Path:
        return self.fs / "instances"

    @property
    def ballast(self) -> Path:
        return self.fs / "ballast"

    @property
    def exec_root(self) -> Path:
        return self.base / "exec"

    @property
    def containerd_state(self) -> Path:
        return self.base / "containerd-state"

    @property
    def socket(self) -> Path:
        return self.base / "docker.sock"

    @property
    def containerd_socket(self) -> Path:
        return self.base / "containerd.sock"

    @property
    def pidfile(self) -> Path:
        return self.base / "dockerd.pid"

    @property
    def daemon_json(self) -> Path:
        return self.base / "daemon.json"

    @property
    def containerd_toml(self) -> Path:
        return self.base / "containerd.toml"


def containerd_argv(layout: Layout) -> list[str]:
    return [
        "containerd",
        "--config",
        str(layout.containerd_toml),
        "--address",
        str(layout.containerd_socket),
        "--root",
        str(layout.containerd_root),
        "--state",
        str(layout.containerd_state),
    ]


def containerd_config(layout: Layout) -> str:
    disabled = ", ".join(f'"{p}"' for p in CONTAINERD_DISABLED)
    return (
        "version = 2\n"
        f"disabled_plugins = [{disabled}]\n"
        f'[grpc]\n  address = "{layout.containerd_socket}"\n'
        f'[ttrpc]\n  address = "{layout.containerd_socket}.ttrpc"\n'
        '[debug]\n  address = ""\n'
        '[metrics]\n  address = ""\n'
    )


def dockerd_argv(layout: Layout, token: str) -> list[str]:
    """A daemon reachable only on its own unix socket, with no network setup."""
    return [
        "dockerd",
        "--host",
        f"unix://{layout.socket}",
        "--data-root",
        str(layout.data_root),
        "--exec-root",
        str(layout.exec_root),
        "--pidfile",
        str(layout.pidfile),
        "--config-file",
        str(layout.daemon_json),
        "--containerd",
        str(layout.containerd_socket),
        "--label",
        f"{MARKER_LABEL}={token}",
        "--exec-opt",
        "native.cgroupdriver=cgroupfs",
        "--bridge",
        "none",
        "--iptables=false",
        "--ip6tables=false",
        "--ip-forward=false",
        "--ip-masq=false",
        "--userland-proxy=false",
    ]


def isolation_failures(
    layout: Layout,
    token: str,
    *,
    spawned_pid: int | None,
    pidfile_pid: int | None,
    info: dict[str, Any],
    engine_id: str | None,
    container_ids: Sequence[str],
    outer_docker_host: str | None,
    fresh: bool = True,
) -> list[str]:
    """Why the daemon is not provably the harness's own (empty: it is).

    `fresh` adds "no containers yet"; teardown re-checks identity without it."""
    fails = []
    base = layout.base.resolve()
    sock = layout.socket.resolve()
    if base not in sock.parents:
        fails.append(f"socket {sock} is not under the harness directory {base}")
    outer = (outer_docker_host or "").removeprefix("unix://")
    if str(sock) in HOST_SOCKETS or (outer and Path(outer).resolve() == sock):
        fails.append(f"socket {sock} is the host's default or DOCKER_HOST daemon")
    if spawned_pid is None or pidfile_pid != spawned_pid:
        fails.append(
            f"pidfile names {pidfile_pid}, not the dockerd the harness spawned"
            f" ({spawned_pid})"
        )
    root = info.get("DockerRootDir")
    if not root or Path(root).resolve() != layout.data_root.resolve():
        fails.append(f"DockerRootDir {root!r} is not {layout.data_root}")
    if f"{MARKER_LABEL}={token}" not in (info.get("Labels") or []):
        fails.append(f"daemon lacks the ownership label {MARKER_LABEL}={token}")
    if not engine_id or info.get("ID") != engine_id:
        fails.append(
            f"daemon ID {info.get('ID')!r} does not match the engine-id in its"
            f" data-root ({engine_id!r})"
        )
    if fresh and (container_ids or info.get("Containers")):
        fails.append(
            f"daemon already has containers ({len(container_ids)} listed,"
            f" {info.get('Containers')} in info)"
        )
    return fails


def bounded_fs_failures(
    layout: Layout, mounts: list[MountEntry], total_bytes: int
) -> list[str]:
    """Why `layout.fs` is not the small loop filesystem on the harness tmpfs."""
    by_point = {m.point: m for m in mounts}
    fails = []
    backing = by_point.get(str(layout.backing))
    if backing is None or backing.fstype != "tmpfs":
        fails.append(f"{layout.backing} is not a tmpfs mount")
    fs = by_point.get(str(layout.fs))
    if fs is None or fs.fstype != "ext4" or not fs.source.startswith("/dev/loop"):
        fails.append(f"{layout.fs} is not an ext4 loop mount")
    if total_bytes > FS_MIB * MIB:
        fails.append(f"{layout.fs} is {total_bytes} bytes, above {FS_MIB} MiB")
    return fails


def mounts_under(base: Path, mounts: list[MountEntry]) -> list[str]:
    """Mount points at or below `base`, deepest first."""
    root = str(base)
    points = {
        m.point for m in mounts if m.point == root or m.point.startswith(root + "/")
    }
    return sorted(points, key=lambda p: (-p.count("/"), p))


# -- the harness ------------------------------------------------------------------


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_checked(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True)


def docker_client(socket: Path) -> Any:
    import docker

    return docker.DockerClient(base_url=f"unix://{socket}", timeout=30)


def syncfs(path: Path) -> None:
    """Flush one filesystem (not the whole host, as `sync` would)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        if ctypes.CDLL(None, use_errno=True).syncfs(fd) != 0:
            raise OSError(ctypes.get_errno(), f"syncfs {path}")
    finally:
        os.close(fd)


def wait_until(check: Callable[[], bool], what: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    # check-tests: allow poll-loop — a starting daemon exposes no event to await
    while time.monotonic() < deadline:
        if check():
            return
        # check-tests: allow sleep — between checks of a real daemon's socket
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {what}")


@dataclass
class Harness:
    layout: Layout
    token: str
    run: Runner = run_checked
    mountinfo: Callable[[], str] = lambda: Path("/proc/self/mountinfo").read_text()
    connect: Callable[[Path], Any] = docker_client
    outer_docker_host: str | None = None
    containerd: Any = None  # subprocess.Popen we spawned
    dockerd: Any = None
    client: Any = None
    verified: bool = False
    created_cgroup_parent: bool = False
    log: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, token: str, **kw: Any) -> Harness:
        base = Path(
            tempfile.mkdtemp(
                prefix="argus-dg-", dir=os.environ.get("ARGUS_DISK_GUARD_RT_TMPDIR")
            )
        )
        (base / MARKER_FILE).write_text(token)
        layout = Layout(base)
        if len(str(layout.instances)) > INSTANCES_ROOT_MAX:
            shutil.rmtree(base)
            raise ValueError(
                f"{layout.instances} is longer than the {INSTANCES_ROOT_MAX}"
                " characters a pool's instances_root allows; use a shorter"
                " ARGUS_DISK_GUARD_RT_TMPDIR"
            )
        return cls(layout, token, **kw)

    def mounts(self) -> list[MountEntry]:
        return parse_mountinfo(self.mountinfo())

    # -- filesystem --

    def mount_fs(self) -> None:
        lay = self.layout
        lay.backing.mkdir()
        lay.fs.mkdir()
        size = (FS_MIB + BACKING_SLACK_MIB) * MIB
        self.run(
            [
                "mount",
                "-t",
                "tmpfs",
                "-o",
                f"size={size},mode=0700",
                "argus-dg",
                str(lay.backing),
            ]
        )
        with lay.image.open("wb") as f:
            f.truncate(FS_MIB * MIB)
        self.run(["mkfs.ext4", "-q", "-F", "-m", "0", "-L", "argus-dg", str(lay.image)])
        self.run(["mount", "-o", "loop", "-t", "ext4", str(lay.image), str(lay.fs)])

    def verify_fs(self) -> None:
        vfs = os.statvfs(self.layout.fs)
        if fails := bounded_fs_failures(
            self.layout, self.mounts(), vfs.f_blocks * vfs.f_frsize
        ):
            raise Refused("; ".join(fails))
        for d in (self.layout.data_dir, self.layout.instances):
            d.mkdir()

    def free_bytes(self) -> int:
        vfs = os.statvfs(self.layout.fs)
        return vfs.f_bavail * vfs.f_frsize

    def set_free(self, target: int) -> int:
        """Grow or shrink the ballast file so the bounded fs has ~`target` free."""
        lay = self.layout
        syncfs(lay.fs)
        size = lay.ballast.stat().st_size if lay.ballast.exists() else 0
        new = max(0, size + self.free_bytes() - target)
        with lay.ballast.open("ab") as f:
            if new < size:
                f.truncate(new)
            elif new > size:
                os.posix_fallocate(f.fileno(), 0, new)
        syncfs(lay.fs)  # ext4 reports freed blocks after the journal commit
        return self.free_bytes()

    # -- daemon --

    def start_daemon(self) -> None:
        lay = self.layout
        self.created_cgroup_parent = not CGROUP_PARENT.exists()
        lay.daemon_json.write_text("{}\n")
        lay.containerd_toml.write_text(containerd_config(lay))
        env = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")}
        with (lay.base / "containerd.log").open("wb") as out:
            self.containerd = subprocess.Popen(
                containerd_argv(lay), stdout=out, stderr=out, env=env
            )
        wait_until(
            lambda: (
                self._alive(self.containerd, "containerd")
                and lay.containerd_socket.exists()
            ),
            "containerd socket",
            START_TIMEOUT_S,
        )
        with (lay.base / "dockerd.log").open("wb") as out:
            self.dockerd = subprocess.Popen(
                dockerd_argv(lay, self.token), stdout=out, stderr=out, env=env
            )
        wait_until(
            lambda: self._alive(self.dockerd, "dockerd") and lay.socket.exists(),
            "dockerd socket",
            START_TIMEOUT_S,
        )
        self.client = self.connect(lay.socket)
        wait_until(self._pings, "dockerd to answer", START_TIMEOUT_S)

    def _alive(self, proc: Any, name: str) -> bool:
        if proc.poll() is not None:
            tail = (self.layout.base / f"{name}.log").read_text(errors="replace")
            raise RuntimeError(f"{name} exited {proc.returncode}: {tail[-2000:]}")
        return True

    def _pings(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def _failures(self, *, fresh: bool) -> list[str]:
        lay = self.layout
        pidfile = read_text(str(lay.pidfile))
        engine = read_text(str(lay.data_root / "engine-id"))
        return isolation_failures(
            lay,
            self.token,
            spawned_pid=self.dockerd.pid if self.dockerd else None,
            pidfile_pid=int(pidfile) if pidfile and pidfile.strip().isdigit() else None,
            info=self.client.info(),
            engine_id=engine.strip() if engine else None,
            container_ids=[c.id for c in self.client.containers.list(all=True)],
            outer_docker_host=self.outer_docker_host,
            fresh=fresh,
        )

    def preflight(self) -> dict[str, Any]:
        """Prove the daemon is the harness's own before anything destructive."""
        if fails := self._failures(fresh=True):
            raise Refused("; ".join(fails))
        self.verified = True
        info = self.client.info()
        return {
            "socket": str(self.layout.socket),
            "docker_root": info.get("DockerRootDir"),
            "engine_id": info.get("ID"),
            "server_version": info.get("ServerVersion"),
            "cgroup_driver": info.get("CgroupDriver"),
            "cgroup_version": info.get("CgroupVersion"),
            "storage_driver": info.get("Driver"),
        }

    def labels(self, **extra: str) -> dict[str, str]:
        return {MARKER_LABEL: self.token, **extra}

    # -- teardown --

    def _stop(self, proc: Any, problems: list[str], name: str) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT_S)
            problems.append(f"{name} ignored SIGTERM and was killed")

    def _try(self, problems: list[str], what: str, argv: list[str]) -> None:
        try:
            self.run(argv)
        except (OSError, subprocess.CalledProcessError) as exc:
            problems.append(f"{what}: {exc}")

    def teardown(self) -> list[str]:
        """Remove what the harness created; return what it could not."""
        problems: list[str] = []
        lay = self.layout
        if self.client is not None:
            # Containers only on a daemon re-proven to be ours, only with our label.
            fails = self._failures(fresh=False) if self.verified else ["unverified"]
            if fails:
                problems.append(
                    "containers left: daemon not re-verified: " + "; ".join(fails)
                )
            else:
                for c in self.client.containers.list(
                    all=True, filters={"label": f"{MARKER_LABEL}={self.token}"}
                ):
                    try:
                        c.remove(force=True)
                    except Exception as exc:
                        problems.append(f"container {c.id[:12]}: {exc}")
            self.client.close()
        self._stop(self.dockerd, problems, "dockerd")
        self._stop(self.containerd, problems, "containerd")
        for point in mounts_under(lay.base, self.mounts()):
            if point == str(lay.backing):
                continue
            self._try(problems, f"unmount {point}", ["umount", point])
        if lay.image.exists():
            try:
                found = self.run(["losetup", "-j", str(lay.image)]).stdout
            except (OSError, subprocess.CalledProcessError) as exc:
                problems.append(f"losetup -j: {exc}")
                found = ""
            for line in found.splitlines():
                dev = line.split(":", 1)[0]
                self._try(problems, f"detach {dev}", ["losetup", "-d", dev])
        if str(lay.backing) in mounts_under(lay.base, self.mounts()):
            self._try(problems, f"unmount {lay.backing}", ["umount", str(lay.backing)])
        if self.created_cgroup_parent:
            try:
                CGROUP_PARENT.rmdir()  # only succeeds when empty
            except OSError as exc:
                problems.append(f"cgroup {CGROUP_PARENT}: {exc}")
        left = mounts_under(lay.base, self.mounts())
        if left:
            problems.append(f"mounts left, directory kept: {left}")
        elif read_text(str(lay.base / MARKER_FILE)) != self.token:
            problems.append(f"{lay.base} lacks the harness marker; not removed")
        else:
            shutil.rmtree(lay.base)
        return problems


@dataclass
class Evidence:
    """What a run observed, written as JSON; never claims what was not seen."""

    observed: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    outcome: str = "incomplete"

    def check(self, name: str, ok: bool, detail: Any = None) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def failed(self) -> list[str]:
        return [c["check"] for c in self.checks if not c["ok"]]

    def finish(self) -> None:
        """`verified` only when checks ran and every one held."""
        if self.outcome == "incomplete":
            self.outcome = (
                "failed"
                if self.failed()
                else "verified"
                if self.checks
                else "no_checks"
            )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "outcome": self.outcome,
                    "observed": self.observed,
                    "checks": self.checks,
                    "unverified": self.unverified,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )


# -- Argus state ------------------------------------------------------------------


POOL = "web"


@dataclass(frozen=True)
class Session:
    instance_id: str
    task_id: str
    attempt: int

    @property
    def group_folder(self) -> str:  # the NanoClaw descendant label value
        return f"argus-{POOL}-{self.instance_id[:8]}"


async def seed_world(
    db: Any, docker_host: str, instances_root: Path, sessions: int, fake_target: Any
) -> list[Session]:
    """A NanoClaw pool on `docker_host` with one claimed task per instance.

    Instance rows come from the real reconciler driven against `fake_target`, so
    no host container is started; the caller starts the real containers."""
    from api_support import insert_agent
    from argus_api.fleet import service as fleet
    from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
    from argus_api.fleet.targets import TargetRegistry
    from argus_api.tasks import service as tasks
    from argus_core.fleet import DockerOptions, NanoclawOptions, PoolUpsert
    from argus_core.tasks import TaskCreate

    registry = TargetRegistry()
    registry.override("docker", docker_host, fake_target)
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            POOL,
            PoolUpsert(
                runtime="nanoclaw",
                image="host:dev",
                desired_replicas=sessions,
                max_replicas=sessions,
                docker=DockerOptions(host=docker_host),
                nanoclaw=NanoclawOptions(
                    install_dir="/opt/nc",
                    instances_root=str(instances_root),
                    agent_image="a:1",
                ),
            ),
            "admin:test",
        )
    for _ in range(2):  # reserve, then confirm
        await reconcile_pool(
            db,
            POOL,
            registry,
            FleetSettings(argus_url="http://x", max_starts_in_flight=9),
        )
    async with db.read() as conn:
        insts = sorted(
            await fleet.list_instances(conn, POOL, False), key=lambda i: i.id
        )
    out = []
    for i, inst in enumerate(insts):
        agent = f"agent-{i}"
        await insert_agent(db, agent)
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE agents SET instance_id = ? WHERE id = ?", (inst.id, agent)
            )
            await tasks.create_task(
                tx, TaskCreate(project="demo", title=f"t{i}"), "admin:test"
            )
            claimed = await tasks.claim(tx, agent, [])
        assert claimed is not None
        out.append(Session(inst.id, claimed.id, claimed.attempt))
    return out
