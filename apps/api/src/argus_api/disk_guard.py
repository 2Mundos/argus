"""Disk guard admission (operations.spec.md 3.5).

Measures free space on the data directory, optional instances root, and local
Docker data root; keeps an in-memory hysteresis snapshot that claim paths read.
Pause/shed are intentionally absent from this slice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from argus_api.config import Settings
from argus_api.db import Database
from argus_api.errors import ConflictError
from argus_api.events import record
from argus_api.ops import MIN_STALE_WINDOW_S, LoopStats

log = logging.getLogger(__name__)

ACTOR = "system"
WARN_EVENT = "disk.warn"
DOCKER_INFO_TIMEOUT_S = 2.0

RootRole = Literal["data", "instances", "docker"]
Applicability = Literal["measured", "unknown", "stale", "not_applicable"]
Band = Literal["ok", "warn", "refuse"]
ClaimAdmission = Literal["admitted", "refused"]

StatFn = Callable[[Path], os.stat_result]
StatvfsFn = Callable[[Path], os.statvfs_result]
DockerRootFn = Callable[[], "DockerRootQuery"]
ClockFn = Callable[[], float]


@dataclass(frozen=True)
class DockerRootQuery:
    """Result of asking a local daemon for DockerRootDir (no paths in visibility)."""

    kind: Literal["path", "unknown", "not_applicable"]
    path: Path | None = None


@dataclass(frozen=True)
class RootSample:
    role: RootRole
    applicability: Applicability
    free_bytes: int | None = None
    st_dev: int | None = None


@dataclass(frozen=True)
class Thresholds:
    warn_bytes: int
    warn_clear_bytes: int
    refuse_bytes: int
    resume_bytes: int
    interval_s: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Thresholds:
        return cls(
            warn_bytes=settings.disk_guard_warn_bytes,
            warn_clear_bytes=settings.disk_guard_warn_clear_bytes,
            refuse_bytes=settings.disk_guard_refuse_bytes,
            resume_bytes=settings.disk_guard_resume_bytes,
            interval_s=settings.disk_guard_interval_s,
        )

    @property
    def stale_after_s(self) -> float:
        return max(3 * self.interval_s, MIN_STALE_WINDOW_S)


@dataclass(frozen=True)
class Snapshot:
    """Redacted view claim paths and authenticated ops read."""

    enabled: bool
    claim_admission: ClaimAdmission
    warn_episode: bool
    roots: tuple[RootSample, ...]
    free_bytes: int | None = None  # worst measured free; never 0 for unknown
    measurement: Literal["measured", "unknown", "stale"] = "unknown"


@dataclass
class _Machine:
    """Pure hysteresis state; unit-tested with an injected clock."""

    warn_episode: bool = False
    refused: bool = True
    last_success_mono: float | None = None
    roots: tuple[RootSample, ...] = ()
    free_bytes: int | None = None
    measurement: Literal["measured", "unknown", "stale"] = "unknown"
    warn_just_entered: bool = False


def _instant_band(free: int, thresholds: Thresholds) -> Band:
    if free < thresholds.refuse_bytes:
        return "refuse"
    if free < thresholds.warn_bytes:
        return "warn"
    return "ok"


def default_docker_root(
    *,
    docker_host: str | None = None,
    socket_path: str = "/var/run/docker.sock",
) -> DockerRootQuery:
    """Local unix-socket daemon only; remote / missing socket → not_applicable."""
    host = docker_host if docker_host is not None else os.environ.get("DOCKER_HOST")
    if host is not None and not host.startswith("unix://"):
        return DockerRootQuery("not_applicable")
    sock = (
        host[len("unix://") :] if host and host.startswith("unix://") else socket_path
    )
    if not sock or not os.path.exists(sock):
        return DockerRootQuery("not_applicable")
    try:
        import docker

        client = docker.DockerClient(
            base_url=f"unix://{sock}", timeout=DOCKER_INFO_TIMEOUT_S
        )
        try:
            info = client.info()
        finally:
            client.close()
        root = info.get("DockerRootDir")
        if not root:
            return DockerRootQuery("unknown")
        return DockerRootQuery("path", Path(root))
    except Exception:
        return DockerRootQuery("unknown")


def measure_path(
    path: Path,
    role: RootRole,
    *,
    stat: StatFn = os.stat,
    statvfs: StatvfsFn = os.statvfs,
    missing_is_absent: bool = False,
) -> RootSample:
    try:
        st = stat(path)
    except FileNotFoundError:
        # An optional root that does not exist yet (first use) holds no data:
        # absent, not a failed measurement.
        return RootSample(role, "not_applicable" if missing_is_absent else "unknown")
    except OSError:
        return RootSample(role, "unknown")
    try:
        vfs = statvfs(path)
    except OSError:
        return RootSample(role, "unknown")
    free = vfs.f_bavail * vfs.f_frsize
    return RootSample(role, "measured", free_bytes=free, st_dev=st.st_dev)


def measure_roots(
    *,
    data_dir: Path,
    instances_root: Path | None,
    docker_root: DockerRootQuery,
    stat: StatFn = os.stat,
    statvfs: StatvfsFn = os.statvfs,
) -> tuple[RootSample, ...]:
    samples: list[RootSample] = [
        measure_path(data_dir, "data", stat=stat, statvfs=statvfs)
    ]
    if instances_root is None:
        samples.append(RootSample("instances", "not_applicable"))
    else:
        samples.append(
            measure_path(
                instances_root,
                "instances",
                stat=stat,
                statvfs=statvfs,
                missing_is_absent=True,
            )
        )
    if docker_root.kind == "not_applicable":
        samples.append(RootSample("docker", "not_applicable"))
    elif docker_root.kind == "unknown" or docker_root.path is None:
        samples.append(RootSample("docker", "unknown"))
    else:
        # A DockerRootDir this process cannot stat does not share its filesystem
        # (the investigation's container case): not applicable, not a failed
        # measurement that would refuse every claim.
        sample = measure_path(docker_root.path, "docker", stat=stat, statvfs=statvfs)
        samples.append(
            RootSample("docker", "not_applicable")
            if sample.applicability == "unknown"
            else sample
        )
    return tuple(samples)


@dataclass(frozen=True)
class _Group:
    st_dev: int
    free_bytes: int
    roles: frozenset[RootRole]
    unknown: bool = False


def group_by_device(samples: tuple[RootSample, ...]) -> list[_Group]:
    """Dedup by st_dev; a group containing data counts as the data dir."""
    by_dev: dict[int, _Group] = {}
    unknowns: list[RootSample] = []
    for s in samples:
        if s.applicability == "not_applicable":
            continue
        if s.applicability == "unknown" or s.st_dev is None or s.free_bytes is None:
            unknowns.append(s)
            continue
        g = by_dev.get(s.st_dev)
        if g is None:
            by_dev[s.st_dev] = _Group(s.st_dev, s.free_bytes, frozenset({s.role}))
        else:
            by_dev[s.st_dev] = _Group(
                s.st_dev, s.free_bytes, g.roles | {s.role}, g.unknown
            )
    groups = list(by_dev.values())
    # Unknown applicable roots participate as their own worst-case group.
    groups.extend(
        _Group(st_dev=-1, free_bytes=0, roles=frozenset({u.role}), unknown=True)
        for u in unknowns
    )
    return groups


def worst_free(
    groups: list[_Group],
) -> tuple[Literal["measured", "unknown"], int | None]:
    """Unknown beats everything; otherwise the minimum free decides."""
    if any(g.unknown for g in groups):
        return "unknown", None
    applicable = [g for g in groups if not g.unknown]
    if not applicable:
        return "unknown", None
    return "measured", min(g.free_bytes for g in applicable)


def advance(
    machine: _Machine,
    samples: tuple[RootSample, ...],
    thresholds: Thresholds,
    *,
    now: float,
) -> _Machine:
    """Pure state transition for one measurement pass."""
    status, free = worst_free(group_by_device(samples))
    warn_episode = machine.warn_episode
    refused = machine.refused
    warn_just_entered = False
    last_success = machine.last_success_mono
    measurement: Literal["measured", "unknown", "stale"] = status

    if status == "measured" and free is not None:
        last_success = now
        if free < thresholds.warn_bytes and not warn_episode:
            warn_episode = True
            warn_just_entered = True
        elif free > thresholds.warn_clear_bytes and warn_episode:
            warn_episode = False
        if free < thresholds.refuse_bytes:
            refused = True
        elif free > thresholds.resume_bytes:
            refused = False
        # else: hold refusal between refuse and resume
        return _Machine(
            warn_episode=warn_episode,
            refused=refused,
            last_success_mono=last_success,
            roots=samples,
            free_bytes=free,
            measurement="measured",
            warn_just_entered=warn_just_entered,
        )

    # Unknown this pass: refuse; hold warn episode; maybe mark stale.
    if last_success is None or (now - last_success) > thresholds.stale_after_s:
        measurement = "stale" if last_success is not None else "unknown"
        roots = tuple(
            replace(s, applicability="stale") if s.applicability == "measured" else s
            for s in samples
        )
    else:
        measurement = "unknown"
        roots = samples
    return _Machine(
        warn_episode=warn_episode,
        refused=True,
        last_success_mono=last_success,
        roots=roots,
        # Hold last measured free for stale visibility; never invent 0 for unknown.
        free_bytes=machine.free_bytes,
        measurement=measurement,
        warn_just_entered=False,
    )


# RootSample.band needs thresholds — make it a function instead.
def root_band(sample: RootSample, thresholds: Thresholds) -> Band | None:
    if sample.free_bytes is None or sample.applicability not in ("measured", "stale"):
        return None
    return _instant_band(sample.free_bytes, thresholds)


@dataclass
class DiskGuard:
    """API-owned guard: measures off-loop, admits from the in-memory snapshot."""

    settings: Settings
    db: Database
    stats: LoopStats
    enabled: bool = True
    thresholds: Thresholds = field(init=False)
    _machine: _Machine = field(default_factory=_Machine)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _stat: StatFn = field(default=os.stat, repr=False)
    _statvfs: StatvfsFn = field(default=os.statvfs, repr=False)
    _docker_root: DockerRootFn = field(default=default_docker_root, repr=False)
    _clock: ClockFn = field(default=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        self.thresholds = Thresholds.from_settings(self.settings)
        self.enabled = self.settings.disk_guard_enabled
        if not self.enabled:
            self._machine = _Machine(
                warn_episode=False,
                refused=False,
                measurement="measured",
                free_bytes=None,
                roots=(
                    RootSample("data", "not_applicable"),
                    RootSample("instances", "not_applicable"),
                    RootSample("docker", "not_applicable"),
                ),
            )

    def snapshot(self) -> Snapshot:
        m = self._machine
        if not self.enabled:
            return Snapshot(
                enabled=False,
                claim_admission="admitted",
                warn_episode=False,
                roots=m.roots,
                free_bytes=None,
                measurement="measured",
            )
        # Re-evaluate staleness against the clock without measuring.
        now = self._clock()
        measurement = m.measurement
        roots = m.roots
        free = m.free_bytes
        refused = m.refused
        if m.last_success_mono is None:
            measurement = "unknown"
            refused = True
        elif (now - m.last_success_mono) > self.thresholds.stale_after_s:
            measurement = "stale"
            refused = True
            roots = tuple(
                replace(s, applicability="stale")
                if s.applicability == "measured"
                else s
                for s in m.roots
            )
        return Snapshot(
            enabled=True,
            claim_admission="refused" if refused else "admitted",
            warn_episode=m.warn_episode,
            roots=roots,
            # Unknown must not look like a measured 0; stale may show last free.
            free_bytes=None if measurement == "unknown" else free,
            measurement=measurement,
        )

    def require_admission(self) -> None:
        """Raise typed `disk_low` when the snapshot refuses new claims."""
        if not self.enabled:
            return
        snap = self.snapshot()
        if snap.claim_admission == "refused":
            raise ConflictError(
                "disk space too low to claim new work",
                code="disk_low",
            )

    def _measure_sync(self) -> tuple[RootSample, ...]:
        instances = (
            Path(self.settings.nanoclaw_instances_root)
            if self.settings.nanoclaw_instances_root
            else None
        )
        docker = self._docker_root()
        return measure_roots(
            data_dir=self.settings.data_dir,
            instances_root=instances,
            docker_root=docker,
            stat=self._stat,
            statvfs=self._statvfs,
        )

    async def measure_once(self) -> Snapshot:
        """One bounded pass; may write a single warn event (never while measuring)."""
        if not self.enabled:
            return self.snapshot()
        samples = await asyncio.to_thread(self._measure_sync)
        async with self._lock:
            prev = self._machine
            self._machine = advance(prev, samples, self.thresholds, now=self._clock())
            entered = self._machine.warn_just_entered
            self._machine.warn_just_entered = False
            snap = self.snapshot()
        if entered:
            async with self.db.write() as tx:
                await record(
                    tx,
                    ACTOR,
                    "system",
                    "disk",
                    WARN_EVENT,
                    {
                        "measurement": snap.measurement,
                        "free_bytes": snap.free_bytes,
                        "claim_admission": snap.claim_admission,
                    },
                )
        return snap

    async def run(self) -> None:
        """Background loop: measure, record LoopStats, continue on errors."""
        while True:
            started = time.monotonic()
            try:
                await self.measure_once()
                self.stats.record(time.monotonic() - started)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("disk guard pass failed")
                self.stats.record(time.monotonic() - started, ok=False)
            await asyncio.sleep(self.thresholds.interval_s)
