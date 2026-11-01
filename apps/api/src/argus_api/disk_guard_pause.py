"""Disk guard pause / resume / shed (operations.spec.md 3.5).

Acts on the bands the guard measured: below refuse (or after repeated failed
measurements) it freezes the descendant session containers of local Docker pools
and flags their claims paused; above resume it thaws them; still paused after the
shed period it stops the single heaviest writer. Docker and cgroup calls run off
the event loop and never inside a write transaction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from argus_core.fleet import Pool

from argus_api.config import Settings
from argus_api.db import Database
from argus_api.disk_guard import PassView
from argus_api.disk_guard_writer_ranking import (
    DirectorySizeSample,
    IoStatSnapshot,
    build_candidates,
    rank_writers,
    read_io_stat,
    sample_instance_directory_bounded,
)
from argus_api.events import record
from argus_api.fleet import runtimes, service
from argus_api.fleet.targets import (
    POOL_LABEL,
    Target,
    TargetError,
    TargetRegistry,
)
from argus_api.tasks import service as tasks

log = logging.getLogger(__name__)

ACTOR = "system"
PAUSE_EVENT = "disk.pause"
RESUME_EVENT = "disk.resume"
SHED_EVENT = "disk.shed"
SHED_STOP_GRACE = timedelta(seconds=10)
DIR_SAMPLE_TIMEOUT_S = 10.0

ClockFn = Callable[[], float]
IoStatFn = Callable[[str], IoStatSnapshot]
DirSampleFn = Callable[[str, str], "asyncio.Future[DirectorySizeSample]"]


def container_io_stat(container_id: str | None) -> IoStatSnapshot:
    """cgroup v2 ``io.stat`` of a Docker container (systemd or cgroupfs driver);
    unreadable (``readable=False``) when the controller or the path is missing."""
    if container_id:
        for tmpl in (
            "/sys/fs/cgroup/system.slice/docker-{}.scope/io.stat",
            "/sys/fs/cgroup/docker/{}/io.stat",
        ):
            path = Path(tmpl.format(container_id))
            if path.is_file():
                return read_io_stat(path)
    return IoStatSnapshot({}, readable=False)


@dataclass
class _Paused:
    """A container this guard froze (or found frozen), with its pause-time samples."""

    target: Target
    ref: str
    container_id: str | None
    claims: list[tuple[str, int]]
    io_before: IoStatSnapshot
    dir_sample: DirectorySizeSample = field(
        default_factory=lambda: DirectorySizeSample(None, "unknown", "unknown")
    )


class PauseController:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        targets: TargetRegistry,
        *,
        clock: ClockFn = time.monotonic,
        io_stat: Callable[[str | None], IoStatSnapshot] = container_io_stat,
        dir_sample=sample_instance_directory_bounded,
    ) -> None:
        self.settings = settings
        self.db = db
        self.targets = targets
        self._clock = clock
        self._io_stat = io_stat
        self._dir_sample = dir_sample
        self._paused: dict[str, _Paused] = {}
        self._fail_streak = 0
        self._renew_at = 0.0
        self._next_shed: float | None = None
        # After a restart containers may still be frozen: look once.
        self._adopt_pending = True

    @property
    def paused_refs(self) -> frozenset[str]:
        return frozenset(self._paused)

    async def step(self, view: PassView) -> None:
        """Act on one measurement pass."""
        s = self.settings
        self._fail_streak = 0 if view.measured else self._fail_streak + 1
        free = view.free_bytes
        below = view.measured and free is not None and free < s.disk_guard_refuse_bytes
        failing = not view.measured and self._fail_streak >= (
            s.disk_guard_fail_pause_count
        )
        if self._adopt_pending:
            self._adopt_pending = False
            await self._sweep(view, freeze=False)
        if below or failing:
            await self._sweep(view, freeze=True)
        elif view.measured and free is not None and free > s.disk_guard_resume_bytes:
            await self._resume(view)
        if not self._paused:
            self._next_shed = None
            return
        await self._renew()
        if below and self._next_shed is not None and self._clock() >= self._next_shed:
            await self._shed(view)

    # -- discovery -------------------------------------------------------------

    async def _local_pools(self) -> list[tuple[Pool, str, Target, set[str]]]:
        """(pool, label key, target, host refs) of nanoclaw pools on a local
        unix-socket daemon; remote daemons and Kubernetes never pause."""
        async with self.db.read() as conn:
            pools = await service.list_pools(conn)
        found = []
        for pool in pools:
            key = runtimes.descendant_label(pool)
            if pool.target != "docker" or key is None or pool.nanoclaw is None:
                continue
            target = self.targets.get(pool.target, *runtimes.target_location(pool))
            try:
                if not (await target.probe()).shares_filesystem:
                    continue
                # The Argus-started host container sends the lease heartbeats.
                hosts = {p.ref for p in await target.list({POOL_LABEL: pool.name})}
            except TargetError:
                log.warning("disk guard: pool %s unreachable", pool.name)
                continue
            found.append((pool, key, target, hosts))
        return found

    async def _owners(self) -> tuple[dict[str, str], dict[str, list[tuple[str, int]]]]:
        """(instance id by 8-char prefix, claimed (task, attempt) by instance id)."""
        async with self.db.read() as conn:
            async with conn.execute(
                "SELECT id FROM instances WHERE state != 'terminated'"
            ) as cur:
                ids = {r["id"][:8]: r["id"] for r in await cur.fetchall()}
            async with conn.execute(
                "SELECT t.id, t.attempt, a.instance_id FROM tasks t"
                " JOIN agents a ON a.id = t.assignee_agent_id"
                " WHERE t.state = 'claimed' AND a.instance_id IS NOT NULL"
                " ORDER BY t.id"
            ) as cur:
                claims: dict[str, list[tuple[str, int]]] = {}
                for r in await cur.fetchall():
                    claims.setdefault(r["instance_id"], []).append(
                        (r["id"], r["attempt"])
                    )
        return ids, claims

    async def _sweep(self, view: PassView, *, freeze: bool) -> None:
        """Track (and with `freeze`, pause) every descendant session container."""
        ids, claims_by_instance = await self._owners()
        fresh: list[_Paused] = []
        for pool, key, target, hosts in await self._local_pools():
            assert pool.nanoclaw is not None
            root = pool.nanoclaw.instances_root
            try:
                containers = await target.list_by_label(key)
            except TargetError:
                log.warning("disk guard: listing %s containers failed", pool.name)
                continue
            for c in containers:
                if c.ref in hosts or c.ref in self._paused or not c.running:
                    continue
                if not c.paused and not freeze:
                    continue
                owner = runtimes.descendant_owner(pool, c.value)
                if owner is None:
                    continue
                before = await asyncio.to_thread(self._io_stat, c.container_id)
                if not c.paused:
                    try:
                        await target.pause(c.ref)
                    except TargetError:
                        log.warning("disk guard: pausing %s failed", c.ref)
                        continue
                instance_id = ids.get(owner)
                entry = _Paused(
                    target,
                    c.ref,
                    c.container_id,
                    claims_by_instance.get(instance_id or "", []),
                    before,
                )
                entry.dir_sample = await self._sample_dir(root, instance_id)
                self._paused[c.ref] = entry
                fresh.append(entry)
        if not fresh:
            return
        if self._next_shed is None:
            self._next_shed = self._clock() + self.settings.disk_guard_shed_after_s
        self._renew_at = 0.0
        claims = [cl for e in fresh for cl in e.claims]
        async with self.db.write() as tx:
            await tasks.mark_paused_disk(tx, claims)
            if freeze:
                await record(
                    tx,
                    ACTOR,
                    "system",
                    "disk",
                    PAUSE_EVENT,
                    {
                        "free_bytes": view.free_bytes,
                        "measurement": "measured" if view.measured else "unknown",
                        "roles": list(view.roles),
                        "containers": len(fresh),
                        "claims": len(claims),
                    },
                )

    async def _sample_dir(
        self, root: str, instance_id: str | None
    ) -> DirectorySizeSample:
        """The one bounded directory-size sample taken when a container is paused."""
        if instance_id is None:
            return DirectorySizeSample(None, "unknown", "unknown")
        return await self._dir_sample(
            root, instance_id, os.geteuid(), timeout_s=DIR_SAMPLE_TIMEOUT_S
        )

    # -- renewal ---------------------------------------------------------------

    async def _renew(self) -> None:
        """Renew paused leases (reason `paused_disk`) at half their TTL, so a late
        pass cannot lapse one; each renewal extends by the claim's `lease_ttl_s`."""
        now = self._clock()
        if now < self._renew_at:
            return
        async with self.db.write() as tx:
            renewed = await tasks.renew_paused_disk(tx)
            ttls = [(r.lease_expires_at - tx.now).total_seconds() for r in renewed]
        self._renew_at = now + (min(ttls) / 2 if ttls else 0)

    # -- resume ----------------------------------------------------------------

    async def _resume(self, view: PassView) -> None:
        """Thaw every container this guard froze and clear the pause. A container
        that fails to thaw keeps its claims paused for the next pass."""
        thawed: list[_Paused] = []
        failed = False
        for entry in list(self._paused.values()):
            try:
                await entry.target.unpause(entry.ref)
            except TargetError:
                log.warning("disk guard: unpausing %s failed", entry.ref)
                failed = True
                continue
            del self._paused[entry.ref]
            thawed.append(entry)
        async with self.db.write() as tx:
            cleared = await tasks.clear_paused_disk(
                tx,
                [cl for e in thawed for cl in e.claims] if failed else None,
            )
            if thawed or cleared:
                await record(
                    tx,
                    ACTOR,
                    "system",
                    "disk",
                    RESUME_EVENT,
                    {
                        "free_bytes": view.free_bytes,
                        "containers": len(thawed),
                        "claims": len(cleared),
                    },
                )
        if not self._paused:
            self._next_shed = None

    # -- shed ------------------------------------------------------------------

    async def _shed(self, view: PassView) -> None:
        """Stop the one heaviest writer and fail its attempt as `env_disk_full`
        without spending it; the rest stay paused until the next period."""
        assert self._next_shed is not None
        self._next_shed = self._clock() + self.settings.disk_guard_shed_after_s
        entries = {e.ref: e for e in self._paused.values() if e.claims}
        if not entries:
            return
        after = {
            ref: await asyncio.to_thread(self._io_stat, e.container_id)
            for ref, e in entries.items()
        }
        ranked = rank_writers(
            build_candidates(
                tuple(entries),
                io_before={ref: e.io_before for ref, e in entries.items()},
                io_after=after,
                directory_bytes={ref: e.dir_sample for ref, e in entries.items()},
            )
        )
        top = ranked[0]
        victim = entries[top.container_id]
        try:
            await victim.target.stop(victim.ref, grace=SHED_STOP_GRACE)
        except TargetError:
            log.warning("disk guard: stopping %s failed", victim.ref)
            return
        del self._paused[victim.ref]
        async with self.db.write() as tx:
            shed = [
                task_id
                for task_id, attempt in victim.claims
                if await tasks.shed_disk_full(tx, task_id, attempt)
            ]
            await record(
                tx,
                ACTOR,
                "system",
                "disk",
                SHED_EVENT,
                {
                    "free_bytes": view.free_bytes,
                    "ranked_by": (
                        "ranked by size"
                        if top.rank_source == "ranked_by_size"
                        else "io.stat wbytes delta"
                    ),
                    "write_delta": top.write_delta,
                    "directory_bytes": top.directory_bytes,
                    "tasks": shed,
                    "reason": tasks.ENV_DISK_FULL,
                },
            )
        if not self._paused:
            self._next_shed = None
