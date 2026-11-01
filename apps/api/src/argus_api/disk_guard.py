"""Disk guard admission (operations.spec.md 3.5).

Measures free space on the data directory, optional instances root, and local
Docker data root; keeps an in-memory hysteresis snapshot that claim paths read.
Pause/resume/shed live in ``disk_guard_pause``; the guard hands it one
``PassView`` per pass. Writer-ranking measurements live in
``disk_guard_writer_ranking``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

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
class PassView:
    """What one guard pass tells the pause controller."""

    measured: bool
    free_bytes: int | None
    roles: tuple[RootRole, ...] = ()


class PauseHook(Protocol):
    async def step(self, view: PassView) -> None: ...


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
    # Internal only: which probe failed when applicability is unknown.
    # Distinguishes "path not visible" (stat → docker not_applicable) from
    # "path visible but free space unreadable" (statvfs → stay unknown).
    probe_error: Literal["stat", "statvfs"] | None = None


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


Measurement = Literal["measured", "unknown", "stale", "disabled"]


@dataclass(frozen=True)
class Snapshot:
    """Redacted view claim paths and authenticated ops read.

    Per-root `band` (via root_band) is the instantaneous free-space band for that
    root. Global `claim_admission` is latched hysteresis: after refuse/unknown/stale
    it stays refused until free clears the resume edge. An operator can therefore
    see a root band of "warn" (or even "ok") next to claim_admission "refused".
    """

    enabled: bool
    claim_admission: ClaimAdmission
    warn_episode: bool
    roots: tuple[RootSample, ...]
    free_bytes: int | None = None  # worst measured free; never 0 for unknown
    measurement: Measurement = "unknown"


@dataclass
class _Machine:
    """Pure hysteresis state; unit-tested with an injected clock."""

    warn_episode: bool = False
    refused: bool = True
    last_success_mono: float | None = None
    roots: tuple[RootSample, ...] = ()
    free_bytes: int | None = None
    measurement: Measurement = "unknown"
    warn_just_entered: bool = False
    # Captured when the episode was entered; used for the warn event payload
    # (including retries after a failed write) so the event reflects entry state.
    warn_entry_free_bytes: int | None = None
    warn_entry_measurement: Measurement | None = None
    warn_entry_claim_admission: ClaimAdmission | None = None


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
        # absent, not a failed measurement. Otherwise this is a failed stat.
        if missing_is_absent:
            return RootSample(role, "not_applicable")
        return RootSample(role, "unknown", probe_error="stat")
    except OSError:
        return RootSample(role, "unknown", probe_error="stat")
    try:
        vfs = statvfs(path)
    except OSError:
        return RootSample(role, "unknown", probe_error="statvfs")
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
        # measurement that would refuse every claim. A stat-able path whose
        # statvfs fails stays unknown and refuses claims.
        sample = measure_path(docker_root.path, "docker", stat=stat, statvfs=statvfs)
        samples.append(
            RootSample("docker", "not_applicable")
            if sample.probe_error == "stat"
            else sample
        )
    return tuple(samples)


@dataclass(frozen=True)
class _Group:
    st_dev: int
    free_bytes: int
    unknown: bool = False


def group_by_device(samples: tuple[RootSample, ...]) -> list[_Group]:
    """Dedup measured roots by st_dev; unknown roots stay their own groups."""
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
            by_dev[s.st_dev] = _Group(s.st_dev, s.free_bytes)
        else:
            # Same device: one filesystem; the worst measured free decides.
            by_dev[s.st_dev] = _Group(s.st_dev, min(g.free_bytes, s.free_bytes))
    groups = list(by_dev.values())
    # Unknown applicable roots participate as their own worst-case group.
    groups.extend(_Group(st_dev=-1, free_bytes=0, unknown=True) for _ in unknowns)
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
    measurement: Measurement = status
    was_stale = (
        last_success is not None and (now - last_success) > thresholds.stale_after_s
    )

    if status == "measured" and free is not None:
        # Snapshot marks stale without latching `_Machine.refused`. Re-entering
        # measured after a stale gap must treat admission as refused so only
        # free > resume clears the dead band (not a mid-band value).
        if was_stale:
            refused = True
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
        # Carry a pending warn flag until the event write succeeds, even if the
        # episode has already cleared (otherwise the episode's only event is lost).
        entry_free = None
        entry_meas: Measurement | None = None
        entry_adm: ClaimAdmission | None = None
        if machine.warn_just_entered:
            warn_just_entered = True
            entry_free = machine.warn_entry_free_bytes
            entry_meas = machine.warn_entry_measurement
            entry_adm = machine.warn_entry_claim_admission
        elif warn_just_entered:
            entry_free = free
            entry_meas = "measured"
            entry_adm = "refused" if refused else "admitted"
        return _Machine(
            warn_episode=warn_episode,
            refused=refused,
            last_success_mono=last_success,
            roots=samples,
            free_bytes=free,
            measurement="measured",
            warn_just_entered=warn_just_entered,
            warn_entry_free_bytes=entry_free,
            warn_entry_measurement=entry_meas,
            warn_entry_claim_admission=entry_adm,
        )

    # Unknown this pass: refuse; hold warn episode; maybe mark stale.
    if last_success is None or was_stale:
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
        # Preserve pending warn until a successful record clears it.
        warn_just_entered=machine.warn_just_entered,
        warn_entry_free_bytes=machine.warn_entry_free_bytes
        if machine.warn_just_entered
        else None,
        warn_entry_measurement=machine.warn_entry_measurement
        if machine.warn_just_entered
        else None,
        warn_entry_claim_admission=machine.warn_entry_claim_admission
        if machine.warn_just_entered
        else None,
    )


def breach_roles(
    samples: tuple[RootSample, ...], refuse_bytes: int
) -> tuple[RootRole, ...]:
    """Roles of the filesystems below refuse (or unknown): the role union per
    `st_dev` group. A group that contains the data directory counts as `data`
    whichever other roles share it (data-directory strictness)."""
    roles: set[RootRole] = set()
    by_dev: dict[int, set[RootRole]] = {}
    free: dict[int, int] = {}
    for s in samples:
        if s.applicability == "not_applicable":
            continue
        if s.st_dev is None or s.free_bytes is None:
            roles.add(s.role)
            continue
        by_dev.setdefault(s.st_dev, set()).add(s.role)
        free[s.st_dev] = min(free.get(s.st_dev, s.free_bytes), s.free_bytes)
    for dev, group in by_dev.items():
        if free[dev] < refuse_bytes:
            roles |= {"data"} if "data" in group else group
    return tuple(sorted(roles))


def root_band(sample: RootSample, thresholds: Thresholds) -> Band | None:
    """Instantaneous per-root band from free bytes; not the latched claim gate.

    Claim admission uses hysteresis (refuse/resume). This band can show "warn"
    or "ok" while global claim_admission is still "refused".
    """
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
    _docker_root: DockerRootFn | None = field(default=None, repr=False)
    _clock: ClockFn = field(default=time.monotonic, repr=False)
    pause: PauseHook | None = None
    # Injectable seam for the warn-event INSERT (tests); default writes via db.
    _write_warn: Callable[[dict[str, Any]], Awaitable[None]] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        self.thresholds = Thresholds.from_settings(self.settings)
        self.enabled = self.settings.disk_guard_enabled
        if self._docker_root is None:
            socket_path = self.settings.nanoclaw_docker_socket
            self._docker_root = lambda: default_docker_root(socket_path=socket_path)
        if not self.enabled:
            self._machine = _Machine(
                warn_episode=False,
                refused=False,
                measurement="disabled",
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
                measurement="disabled",
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
        assert self._docker_root is not None
        docker = self._docker_root()
        return measure_roots(
            data_dir=self.settings.data_dir,
            instances_root=instances,
            docker_root=docker,
            stat=self._stat,
            statvfs=self._statvfs,
        )

    def _all_unknown_samples(self) -> tuple[RootSample, ...]:
        """Synthetic fail-closed pass when measurement raises unexpectedly."""
        instances: RootSample = (
            RootSample("instances", "unknown")
            if self.settings.nanoclaw_instances_root
            else RootSample("instances", "not_applicable")
        )
        return (
            RootSample("data", "unknown"),
            instances,
            RootSample("docker", "unknown"),
        )

    async def _write_warn_event(self, payload: dict[str, Any]) -> None:
        """Persist one disk.warn event. Overridable via `_write_warn` for tests."""
        if self._write_warn is not None:
            await self._write_warn(payload)
            return
        async with self.db.write() as tx:
            await record(
                tx,
                ACTOR,
                "system",
                "disk",
                WARN_EVENT,
                payload,
            )

    async def measure_once(self) -> Snapshot:
        """One bounded pass; may write a single warn event (never while measuring).

        Sampling and admission advance always commit to `_machine`. A warn-event
        INSERT failure is logged and leaves the pending entry for exactly-once
        retry; it does not fail the pass (so API startup stays up when only the
        event write is unhealthy).
        """
        if not self.enabled:
            return self.snapshot()
        try:
            samples = await asyncio.to_thread(self._measure_sync)
        except Exception:
            log.exception("disk guard measurement failed")
            samples = self._all_unknown_samples()
        # Hold the lock across advance + optional event write so concurrent
        # measure_once calls cannot both take the same pending warn entry.
        async with self._lock:
            prev = self._machine
            self._machine = advance(prev, samples, self.thresholds, now=self._clock())
            snap = self.snapshot()
            if self._machine.warn_just_entered:
                payload = {
                    "measurement": self._machine.warn_entry_measurement,
                    "free_bytes": self._machine.warn_entry_free_bytes,
                    "claim_admission": self._machine.warn_entry_claim_admission,
                }
                try:
                    await self._write_warn_event(payload)
                except Exception:
                    # Leave warn_just_entered set so the next pass retries the
                    # entry snapshot exactly once; measurement already advanced.
                    log.exception("disk guard warn event write failed")
                else:
                    # Clear only after a successful record so a failed write can
                    # retry without losing the episode's only event (and without
                    # duplicating).
                    self._machine.warn_just_entered = False
                    self._machine.warn_entry_free_bytes = None
                    self._machine.warn_entry_measurement = None
                    self._machine.warn_entry_claim_admission = None
        return snap

    def pass_view(self) -> PassView:
        m = self._machine
        return PassView(
            measured=m.measurement == "measured",
            free_bytes=m.free_bytes if m.measurement == "measured" else None,
            roles=breach_roles(m.roots, self.thresholds.refuse_bytes),
        )

    async def run_pass(self) -> None:
        """One measurement, then the pause controller's reaction to it."""
        await self.measure_once()
        if self.pause is not None:
            await self.pause.step(self.pass_view())

    async def run(self) -> None:
        """Background loop: measure, record LoopStats, continue on errors."""
        while True:
            # The lifespan already measured once; wait one interval before the
            # next pass so startup is not measured twice.
            await asyncio.sleep(self.thresholds.interval_s)
            started = time.monotonic()
            try:
                await self.run_pass()
                self.stats.record(time.monotonic() - started)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("disk guard pass failed")
                self.stats.record(time.monotonic() - started, ok=False)
