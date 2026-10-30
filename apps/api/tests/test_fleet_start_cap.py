"""Start-rate cap and deadlines (worker lifecycle 3.5)."""

import asyncio
import os
from datetime import timedelta

import pytest
from api_support import FakeClock, FakeTarget
from argus_api.db import Database
from argus_api.fleet import service as fleet
from argus_api.fleet import targets
from argus_api.fleet.reconciler import FleetSettings, default_start_cap, reconcile
from argus_api.fleet.targets import DockerTarget, TargetError, TargetRegistry
from argus_core.auth import Scope
from argus_core.fleet import PoolUpsert

URL = "http://argus.test:8000"


def settings(cap: int) -> FleetSettings:
    return FleetSettings(argus_url=URL, max_starts_in_flight=cap)


@pytest.fixture
def target() -> FakeTarget:
    return FakeTarget()


@pytest.fixture
def registry(target: FakeTarget) -> TargetRegistry:
    r = TargetRegistry()
    r.override("docker", None, target)
    return r


async def make_pool(db: Database, name: str, desired: int) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            name,
            PoolUpsert(image="worker:1", desired_replicas=desired, max_replicas=10),
            "admin:test",
        )


async def states(db: Database, pool: str | None = None) -> list[str]:
    async with db.read() as conn:
        pools = [pool] if pool else [p.name for p in await fleet.list_pools(conn)]
        return [
            str(i.state) for p in pools for i in await fleet.list_instances(conn, p)
        ]


# -- the cap ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cpus", "cap"), [(1, 1), (2, 1), (3, 1), (4, 2), (5, 2), (None, 1)]
)
def test_start_cap_is_half_the_logical_cpus_at_least_one(
    monkeypatch: pytest.MonkeyPatch, cpus: int | None, cap: int
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)
    assert default_start_cap() == cap
    assert FleetSettings(argus_url=URL).max_starts_in_flight == cap


async def test_cap_limits_new_starts_per_pass(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, "web", 5)
    s = settings(2)
    await reconcile(db, registry, s)
    assert len(target.started) == 2
    assert await states(db) == ["reserved", "reserved"]
    await reconcile(db, registry, s)  # confirms two, reserves and starts two more
    assert len(target.started) == 4
    assert sorted(await states(db)) == ["reserved", "reserved", "running", "running"]
    await reconcile(db, registry, s)
    await reconcile(db, registry, s)
    assert len(target.started) == 5
    assert sorted(await states(db)) == ["running"] * 5


async def test_cap_is_reconciler_wide_across_pools(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, "a", 3)
    await make_pool(db, "b", 3)
    s = settings(2)
    await reconcile(db, registry, s)  # pool a takes the whole budget
    assert len(await states(db, "a")) == 2
    assert await states(db, "b") == []
    assert len(target.started) == 2
    await reconcile(db, registry, s)  # a confirmed; budget goes to a (one) and b (one)
    assert len(target.started) == 4
    assert len(await states(db, "a")) == 3
    assert len(await states(db, "b")) == 1


async def test_pending_reservations_hold_the_budget_until_a_listing_confirms(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, "web", 3)
    s = settings(2)
    target.fail_start = True
    await reconcile(db, registry, s)
    clock.advance(60)
    await reconcile(db, registry, s)  # retries the two; no third reservation
    assert await states(db) == ["reserved", "reserved"]
    assert target.containers == {}
    target.fail_start = False
    clock.advance(60)
    await reconcile(db, registry, s)  # starts succeed, but are unconfirmed
    assert await states(db) == ["reserved", "reserved"]
    assert len(target.started) == 2
    await reconcile(db, registry, s)  # the listing confirms; budget frees
    assert sorted(await states(db)) == ["reserved", "running", "running"]


async def test_ambiguous_start_keeps_the_reservation_and_never_duplicates(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, "web", 1)
    s = settings(1)
    target.ambiguous_start = True
    await reconcile(db, registry, s)
    assert await states(db) == ["reserved"]  # occupies the only slot
    clock.advance(60)
    target.ambiguous_start = False
    await reconcile(db, registry, s)  # the listing finds it; no second start
    assert len(target.containers) == 1
    assert len(target.started) == 1
    assert await states(db) == ["running"]


async def test_running_instances_do_not_count_against_the_cap(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, "web", 2)
    s = settings(1)
    await reconcile(db, registry, s)
    await reconcile(db, registry, s)
    await reconcile(db, registry, s)
    assert await states(db) == ["running", "running"]
    async with db.write() as tx:
        await fleet.scale(tx, "web", 3, "test", Scope.ADMIN)
    await reconcile(db, registry, s)  # two running; a third start still fits
    assert len(target.started) == 3


# -- deadlines -------------------------------------------------------------------------


def test_deadline_constants() -> None:
    assert targets.PROVIDER_OP_TIMEOUT_S == 30.0
    assert targets.START_TIMEOUT_S == 120.0


async def test_docker_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    real = asyncio.wait_for

    async def spy(aw, timeout):
        seen.append(timeout)
        return await real(aw, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)
    t = DockerTarget()
    monkeypatch.setattr(t, "_start", lambda spec: "ref")
    monkeypatch.setattr(t, "_list", lambda labels: [])
    monkeypatch.setattr(t, "_stop", lambda ref, grace: None)
    await t.start(object())  # type: ignore[arg-type]
    await t.list({})
    await t.stop("x", grace=timedelta(seconds=30))
    assert seen == [120.0, 30.0, 60.0]  # stop: its grace plus the ordinary deadline


async def test_a_slow_provider_call_becomes_a_target_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    monkeypatch.setattr(targets, "START_TIMEOUT_S", 0.05)
    t = DockerTarget()
    monkeypatch.setattr(t, "_start", lambda spec: time.sleep(0.3))
    with pytest.raises(TargetError, match="timed out"):
        await t.start(object())  # type: ignore[arg-type]


async def test_a_start_timeout_is_ambiguous_and_keeps_the_reservation(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    """A start that outlives its deadline may still have created the container."""
    import time

    def slow_create(spec) -> str:
        time.sleep(0.2)
        target.containers[spec.name] = {
            "instance_id": spec.instance_id,
            "pool": spec.labels["argus.pool"],
            "status": "running",
            "exit_code": None,
        }
        target.started.append(spec.name)
        return spec.name

    docker = DockerTarget()

    async def start(spec) -> str:
        return await docker._call(slow_create, spec, deadline=0.05)

    target.start = start  # type: ignore[method-assign]
    await make_pool(db, "web", 1)
    s = settings(1)
    await reconcile(db, registry, s)
    assert await states(db) == ["reserved"]  # timed out: kept, not failed
    await asyncio.sleep(0.3)  # the abandoned thread finishes the create
    clock.advance(10)
    await reconcile(db, registry, s)  # resolved by instance id from the listing
    assert await states(db) == ["running"]
    assert len(target.started) == 1
