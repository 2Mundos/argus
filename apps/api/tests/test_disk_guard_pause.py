"""Disk guard pause / resume / shed (operations.spec.md 3.5).

Synthetic: a fake Docker target, injected io.stat and directory samples, fake
clocks. Nothing here fills a disk or talks to a real daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from api_support import FakeClock, FakeTarget, insert_agent
from argus_api.agents import service as agents
from argus_api.config import Settings
from argus_api.db import Database, ts
from argus_api.disk_guard import PassView, RootSample, breach_roles
from argus_api.disk_guard_pause import PauseController
from argus_api.disk_guard_writer_ranking import DirectorySizeSample, IoStatSnapshot
from argus_api.errors import ConflictError
from argus_api.fleet import service as fleet
from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
from argus_api.fleet.targets import TargetRegistry
from argus_api.tasks import service as tasks
from argus_api.tasks.views import read_task
from argus_core.fleet import NanoclawOptions, PoolUpsert
from argus_core.tasks import TaskCreate
from test_disk_guard import REFUSE, RESUME, WARN, guard_for, settings_for

SHED_S = 30.0
LOW = REFUSE - 1
OK = RESUME + 1
DEAD = REFUSE + 1  # between refuse and resume


@pytest.fixture
def target() -> FakeTarget:
    t = FakeTarget()
    t.shares_filesystem = True
    return t


@pytest.fixture
def registry(target: FakeTarget) -> TargetRegistry:
    r = TargetRegistry()
    r.override("docker", None, target)
    return r


@dataclass
class Session:
    instance_id: str
    child: str  # the session container
    task_id: str
    attempt: int
    container_id: str


@dataclass
class World:
    db: Database
    clock: FakeClock
    target: FakeTarget
    registry: TargetRegistry
    settings: Settings
    mono: list[float] = field(default_factory=lambda: [1000.0])
    wbytes: dict[str, int] = field(default_factory=dict)
    dir_bytes: dict[str, int | None] = field(default_factory=dict)
    dir_calls: list[str] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)

    def controller(self) -> PauseController:
        async def dir_sample(root, instance_id, uid, *, timeout_s):
            self.dir_calls.append(instance_id)
            size = self.dir_bytes.get(instance_id)
            return DirectorySizeSample(
                size, "measured" if size is not None else "unknown", "measured"
            )

        def io_stat(container_id):
            if container_id not in self.wbytes:
                return IoStatSnapshot({}, readable=False)
            return IoStatSnapshot({"8:0": self.wbytes[container_id]}, readable=True)

        return PauseController(
            self.settings,
            self.db,
            self.registry,
            clock=lambda: self.mono[0],
            io_stat=io_stat,
            dir_sample=dir_sample,
        )

    def tick(self, seconds: float) -> None:
        self.mono[0] += seconds
        self.clock.advance(seconds)

    async def task(self, task_id: str):
        async with self.db.read() as conn:
            return await read_task(conn, task_id, self.clock.now)

    async def row(self, task_id: str):
        async with (
            self.db.read() as conn,
            conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur,
        ):
            return await cur.fetchone()

    async def events(self, kind: str) -> list[dict]:
        async with (
            self.db.read() as conn,
            conn.execute("SELECT data FROM events WHERE kind = ?", (kind,)) as cur,
        ):
            import json

            return [json.loads(r["data"]) for r in await cur.fetchall()]


async def make_world(
    db: Database,
    clock: FakeClock,
    target: FakeTarget,
    registry: TargetRegistry,
    tmp_path: Path,
    sessions: int = 2,
    **settings: object,
) -> World:
    world = World(
        db,
        clock,
        target,
        registry,
        settings_for(
            tmp_path,
            disk_guard_pause=True,
            disk_guard_shed_after_s=SHED_S,
            disk_guard_fail_pause_count=2,
            **settings,
        ),
    )
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(
                runtime="nanoclaw",
                image="host:dev",
                desired_replicas=sessions,
                max_replicas=sessions,
                nanoclaw=NanoclawOptions(
                    install_dir="/opt/nc", instances_root="/srv/i", agent_image="a:1"
                ),
            ),
            "admin:test",
        )
    for _ in range(2):
        await reconcile_pool(
            db,
            "web",
            registry,
            FleetSettings(argus_url="http://x", max_starts_in_flight=9),
        )
    async with db.read() as conn:
        insts = await fleet.list_instances(conn, "web", False)
    assert len(insts) == sessions
    for i, inst in enumerate(sorted(insts, key=lambda x: x.id)):
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
        child, cid = f"ncl-{i}", f"cid{i}" + "0" * 20
        target.children[child] = {
            "label": "nanoclaw-group-folder",
            "value": f"argus-web-{inst.id[:8]}",
            "container_id": cid,
        }
        world.sessions.append(Session(inst.id, child, claimed.id, claimed.attempt, cid))
    return world


@pytest.fixture
async def world(db, clock, target, registry, tmp_path) -> World:
    return await make_world(db, clock, target, registry, tmp_path)


def view(free: int | None, *, measured: bool = True) -> PassView:
    return PassView(measured=measured, free_bytes=free, roles=("data",))


# -- B: refuse band pauses descendants only -----------------------------------------


async def test_refuse_band_pauses_session_containers_not_the_host(
    world: World,
) -> None:
    host = next(iter(world.target.containers))
    # A host container that happens to carry the descendant label is still safe.
    world.target.children[host] = {
        "label": "nanoclaw-group-folder",
        "value": "argus-web-" + world.sessions[0].instance_id[:8],
    }
    ctrl = world.controller()
    await ctrl.step(view(DEAD))  # not below refuse: nothing
    assert world.target.paused == []

    await ctrl.step(view(LOW))
    assert sorted(world.target.paused) == ["ncl-0", "ncl-1"]
    assert host not in world.target.paused
    for s in world.sessions:
        assert (await world.row(s.task_id))["paused_disk_at"] is not None
        assert (await world.task(s.task_id)).stall_cause == "paused_disk"
    assert sorted(world.dir_calls) == sorted(s.instance_id for s in world.sessions)

    await ctrl.step(view(LOW))  # already paused: no second call, no second sample
    assert len(world.target.paused) == 2 and len(world.dir_calls) == 2
    assert len(await world.events("disk.pause")) == 1


async def test_remote_daemon_is_never_paused(world: World) -> None:
    world.target.shares_filesystem = False
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    assert world.target.paused == []
    assert (await world.row(world.sessions[0].task_id))["paused_disk_at"] is None


async def test_guard_refuses_claims_and_pauses_in_one_pass(
    world: World, db: Database, tmp_path: Path
) -> None:
    guard = guard_for(db, tmp_path, frees={"data": LOW})
    guard.pause = world.controller()
    await guard.run_pass()
    with pytest.raises(ConflictError) as exc:
        guard.require_admission()
    assert exc.value.code == "disk_low"
    assert sorted(world.target.paused) == ["ncl-0", "ncl-1"]


def test_group_with_the_data_directory_counts_as_data() -> None:
    def sample(role, dev, free):
        return RootSample(role, "measured", free_bytes=free, st_dev=dev)

    samples = (
        sample("data", 1, LOW),
        sample("instances", 1, WARN),
        sample("docker", 2, LOW),
    )
    assert breach_roles(samples, REFUSE) == ("data", "docker")
    unknown = (sample("data", 1, WARN), RootSample("docker", "unknown"))
    assert breach_roles(unknown, REFUSE) == ("docker",)


# -- C: resume edge ---------------------------------------------------------------


async def test_resume_only_above_the_resume_edge(world: World) -> None:
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    await ctrl.step(view(DEAD))  # still below the 2.5 GiB band: stay paused
    await ctrl.step(view(RESUME))  # equality does not resume
    assert world.target.unpaused == []
    for s in world.sessions:
        assert (await world.row(s.task_id))["paused_disk_at"] is not None

    world.tick(10)
    await ctrl.step(view(OK))
    assert sorted(world.target.unpaused) == ["ncl-0", "ncl-1"]
    for s in world.sessions:
        row = await world.row(s.task_id)
        assert row["paused_disk_at"] is None and row["quiet_at"] is None
        assert row["activity_at"] == ts(world.clock.now)
    assert len(await world.events("disk.resume")) == 1


async def test_resume_after_restart_thaws_containers_found_paused(
    world: World,
) -> None:
    first = world.controller()
    await first.step(view(LOW))
    restarted = world.controller()  # in-memory state lost, containers still frozen
    await restarted.step(view(OK))
    assert sorted(world.target.unpaused) == ["ncl-0", "ncl-1"]
    assert (await world.row(world.sessions[0].task_id))["paused_disk_at"] is None


async def test_failed_unpause_keeps_that_claim_paused(world: World) -> None:
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    real = world.target.unpause

    async def flaky(ref):
        if ref == "ncl-1":
            from argus_api.fleet.targets import TargetError

            raise TargetError("boom")
        await real(ref)

    world.target.unpause = flaky  # type: ignore[method-assign]
    await ctrl.step(view(OK))
    assert (await world.row(world.sessions[0].task_id))["paused_disk_at"] is None
    assert (await world.row(world.sessions[1].task_id))["paused_disk_at"] is not None
    world.target.unpause = real  # type: ignore[method-assign]
    await ctrl.step(view(OK))
    assert (await world.row(world.sessions[1].task_id))["paused_disk_at"] is None


# -- D: shed ------------------------------------------------------------------------


async def test_shed_stops_the_heaviest_writer_once_per_period(world: World) -> None:
    s0, s1 = world.sessions
    world.wbytes |= {s0.container_id: 100, s1.container_id: 100}
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    world.wbytes |= {s0.container_id: 150, s1.container_id: 900}  # s1 wrote more
    world.tick(SHED_S - 1)
    await ctrl.step(view(LOW))
    assert world.target.stopped == []  # not yet

    world.tick(1)
    await ctrl.step(view(LOW))
    assert world.target.stopped == ["ncl-1"]
    shed = await world.task(s1.task_id)
    assert shed.state == "ready" and shed.failures == 0  # attempt not consumed
    assert (await world.row(s1.task_id))["paused_disk_at"] is None
    assert (await world.row(s0.task_id))["paused_disk_at"] is not None
    assert world.target.children["ncl-0"]["paused"] is True  # others stay paused
    [event] = await world.events("disk.shed")
    assert event["ranked_by"] == "io.stat wbytes delta"
    assert event["reason"] == "env_disk_full" and event["write_delta"] == 800
    async with (
        world.db.read() as conn,
        conn.execute(
            "SELECT outcome, outcome_reason FROM task_attempts WHERE task_id = ?",
            (s1.task_id,),
        ) as cur,
    ):
        assert tuple(await cur.fetchone()) == ("released", "env_disk_full")

    # At most one per period: the survivor is only shed a full period later.
    world.tick(SHED_S - 1)
    await ctrl.step(view(LOW))
    assert world.target.stopped == ["ncl-1"]
    world.tick(1)
    await ctrl.step(view(LOW))
    assert world.target.stopped == ["ncl-1", "ncl-0"]
    assert len(await world.events("disk.shed")) == 2


async def test_shed_breaks_io_ties_by_directory_size(world: World) -> None:
    s0, s1 = world.sessions
    world.dir_bytes |= {s0.instance_id: 10, s1.instance_id: 5_000}
    world.wbytes |= {s0.container_id: 1, s1.container_id: 1}
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    world.wbytes |= {s0.container_id: 51, s1.container_id: 51}  # equal deltas
    world.tick(SHED_S)
    await ctrl.step(view(LOW))
    assert world.target.stopped == ["ncl-1"]


async def test_shed_falls_back_to_size_when_io_stat_is_unreadable(
    world: World,
) -> None:
    s0, s1 = world.sessions
    world.dir_bytes |= {s0.instance_id: 9_000, s1.instance_id: 5}  # no io.stat at all
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    world.tick(SHED_S)
    await ctrl.step(view(LOW))
    assert world.target.stopped == ["ncl-0"]
    [event] = await world.events("disk.shed")
    assert event["ranked_by"] == "ranked by size"


async def test_no_shed_between_refuse_and_resume_or_after_resume(
    world: World,
) -> None:
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    world.tick(SHED_S * 3)
    await ctrl.step(view(DEAD))  # above refuse: held paused, not shed
    assert world.target.stopped == []
    await ctrl.step(view(OK))
    world.tick(SHED_S * 3)
    await ctrl.step(view(LOW))  # a new episode starts its own period
    assert world.target.stopped == []


# -- E: failed measurement ---------------------------------------------------------


async def test_failed_measurement_pauses_only_after_consecutive_failures(
    world: World,
) -> None:
    ctrl = world.controller()
    await ctrl.step(view(None, measured=False))
    assert world.target.paused == []
    await ctrl.step(view(OK))  # a good pass resets the streak
    await ctrl.step(view(None, measured=False))
    assert world.target.paused == []
    await ctrl.step(view(None, measured=False))
    assert sorted(world.target.paused) == ["ncl-0", "ncl-1"]
    world.tick(SHED_S * 2)
    await ctrl.step(view(None, measured=False))
    assert world.target.stopped == []  # unknown is not "below refuse": never shed


# -- F: paused claims -------------------------------------------------------------


async def test_guard_renews_paused_leases_and_reaper_skips_them(
    world: World,
) -> None:
    from argus_api import reaper

    s0 = world.sessions[0]
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    ttl = (await world.row(s0.task_id))["lease_ttl_s"]
    for _ in range(6):  # far beyond the lease, quiet and stall windows
        world.tick(ttl / 2)
        await ctrl.step(view(DEAD))  # held paused, below the shed trigger
        async with world.db.write() as tx:
            await agents.heartbeat(tx, "agent-0", [(s0.task_id, s0.attempt)])
            await reaper.reap(tx, quiet_claim_s=int(ttl), stall_fail_s=int(ttl))
    task = await world.task(s0.task_id)
    row = await world.row(s0.task_id)
    assert task.state == "claimed" and row["quiet_at"] is None
    assert task.stall_cause == "paused_disk"
    assert row["lease_expires_at"] > ts(world.clock.now)


async def test_lease_already_expired_is_not_revived(world: World) -> None:
    ctrl = world.controller()
    await ctrl.step(view(LOW))
    s0 = world.sessions[0]
    ttl = (await world.row(s0.task_id))["lease_ttl_s"]
    world.tick(ttl + 1)  # the guard missed its renewal window
    await ctrl.step(view(DEAD))
    row = await world.row(s0.task_id)
    assert row["lease_expires_at"] < ts(world.clock.now)


# -- G: settings -----------------------------------------------------------------


def test_pause_settings_validate_at_startup_and_empty_means_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", "/tmp")
    for name in (
        "ARGUS_DISK_GUARD_PAUSE",
        "ARGUS_DISK_GUARD_SHED_AFTER_S",
        "ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT",
    ):
        monkeypatch.setenv(name, " ")
    s = Settings.from_env()
    assert (s.disk_guard_pause, s.disk_guard_shed_after_s) == (False, 30.0)
    assert s.disk_guard_fail_pause_count == 2

    monkeypatch.setenv("ARGUS_DISK_GUARD_PAUSE", "on")
    monkeypatch.setenv("ARGUS_DISK_GUARD_SHED_AFTER_S", "45")
    monkeypatch.setenv("ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT", "3")
    s = Settings.from_env()
    assert (s.disk_guard_pause, s.disk_guard_shed_after_s) == (True, 45.0)
    assert s.disk_guard_fail_pause_count == 3

    for name, bad in (
        ("ARGUS_DISK_GUARD_PAUSE", "maybe"),
        ("ARGUS_DISK_GUARD_SHED_AFTER_S", "0"),
        ("ARGUS_DISK_GUARD_SHED_AFTER_S", "soon"),
        ("ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT", "0"),
        ("ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT", "1.5"),
    ):
        with monkeypatch.context() as m:
            m.setenv(name, bad)
            with pytest.raises(ValueError, match=name):
                Settings.from_env()
