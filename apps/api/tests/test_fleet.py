import pytest
from api_support import DONE, FakeClock, FakeTarget
from argus_api.agents import service as agents
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.decisions import service as decisions
from argus_api.errors import UnauthorizedError
from argus_api.fleet import service as fleet
from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
from argus_api.fleet.targets import TargetRegistry, container_name
from argus_api.reaper import reap
from argus_api.tasks import service as tasks
from argus_core.agents import AgentRegister
from argus_core.auth import Scope
from argus_core.decisions import DecisionAnswer
from argus_core.fleet import PoolUpsert
from argus_core.tasks import TaskCreate

SETTINGS = FleetSettings(argus_url="http://argus.test:8000")
ADMIN = Identity("t", Scope.ADMIN, None, "test")


@pytest.fixture
def target() -> FakeTarget:
    return FakeTarget()


@pytest.fixture
def registry(target: FakeTarget) -> TargetRegistry:
    r = TargetRegistry()
    r.override("docker", None, target)
    return r


async def make_pool(db: Database, desired: int, max_replicas: int = 5, **kw) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(
                image="worker:1",
                desired_replicas=desired,
                max_replicas=max_replicas,
                **kw,
            ),
            "admin:test",
        )


async def tick(db: Database, registry: TargetRegistry, settings=SETTINGS) -> None:
    await reconcile_pool(db, "web", registry, settings)


async def instances(db: Database, include_terminated: bool = False):
    async with db.read() as conn:
        return await fleet.list_instances(conn, "web", include_terminated)


async def register_from(db: Database, target: FakeTarget, name: str) -> str:
    """What the worker in the container does: redeem its bootstrap token."""
    token = target.specs[name].env["ARGUS_BOOTSTRAP_TOKENS"].split(",")[0]
    async with db.write() as tx:
        reg = await agents.register(tx, AgentRegister(bootstrap_token=token))
    return reg.agent.id


async def scale(db: Database, desired: int, scope: Scope = Scope.ADMIN):
    async with db.write() as tx:
        return await fleet.scale(tx, "web", desired, "test", scope)


# -- scaling up ---------------------------------------------------------------------


async def test_scale_up_starts_instances_with_working_bootstrap_tokens(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=2, capabilities=["python"], env={"MODE": "x"})
    await tick(db, registry)
    live = await instances(db)
    assert [i.state for i in live] == ["running", "running"]
    assert len(target.started) == 2
    spec = target.specs[live[0].provider_ref]
    assert spec.env["ARGUS_URL"] == "http://argus.test:8000"
    assert (spec.env["ARGUS_CAPABILITIES"], spec.env["MODE"]) == ("python", "x")
    agent_id = await register_from(db, target, live[0].provider_ref)
    async with db.read() as conn:
        assert (await agents.get_agent(conn, agent_id)).instance_id == live[0].id
    await tick(db, registry)
    assert len(target.started) == 2  # converged: nothing more to do


async def test_no_public_url_means_no_starts(
    db: Database, registry: TargetRegistry
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry, FleetSettings(argus_url=None))
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert (pool.status, pool.live_instances) == ("failing", 0)
    assert "ARGUS_PUBLIC_URL" in (pool.last_error or "")


async def test_ambiguous_start_never_creates_a_second_container(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    target.ambiguous_start = True
    await tick(db, registry)
    [inst] = await instances(db)
    assert inst.state == "reserved"  # unknown outcome: slot kept, not confirmed
    assert inst.last_error
    target.ambiguous_start = False
    clock.advance(5)
    await tick(db, registry)  # the lookup finds it
    [inst] = await instances(db)
    assert inst.state == "running"
    assert len(target.containers) == 1


async def test_failing_starts_back_off(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    target.fail_start = True
    await tick(db, registry)
    await tick(db, registry)  # too soon to retry
    [inst] = await instances(db)
    assert inst.start_attempts == 1
    clock.advance(5)
    await tick(db, registry)
    [inst] = await instances(db)
    assert inst.start_attempts == 2
    async with db.read() as conn:
        assert (await fleet.get_pool(conn, "web")).status == "failing"


# -- provider failures ------------------------------------------------------------------


async def test_unreachable_target_changes_nothing(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    target.reachable = False
    await scale(db, 3)
    await tick(db, registry)
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert (pool.status, pool.live_instances) == ("unreachable", 1)
    [inst] = await instances(db)
    assert inst.state == "running"  # not presumed dead
    target.reachable = True
    await tick(db, registry)
    assert len(await instances(db)) == 3


async def test_lost_agent_terminates_and_keeps_the_slot_until_confirmed(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    [inst] = await instances(db)
    agent_id = await register_from(db, target, inst.provider_ref)
    clock.advance(91)
    async with db.write() as tx:
        assert (await reap(tx)).lost_agents == [agent_id]
    target.fail_stop = True
    await tick(db, registry)
    [inst] = await instances(db)
    assert inst.state == "terminating"
    assert len(await instances(db)) == 1  # no replacement while unconfirmed
    await tick(db, registry)  # stop retried, still failing
    assert len(await instances(db)) == 1
    target.fail_stop = False
    await tick(db, registry)  # stop succeeds; not yet confirmed
    [still] = await instances(db)
    assert still.id == inst.id
    await tick(db, registry)  # the listing confirms it is gone; replace it
    live = await instances(db)
    assert len(live) == 1
    assert live[0].id != inst.id


async def test_unexpected_exit_ends_agents_and_replaces(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    [inst] = await instances(db)
    agent_id = await register_from(db, target, inst.provider_ref)
    async with db.write() as tx:
        token = (await agents.create_bootstrap_token(tx, _boot(inst.id), "t")).token
    target.exit(inst.provider_ref, code=137)
    await tick(db, registry)
    [old] = [i for i in await instances(db, True) if i.id == inst.id]
    assert (old.state, old.exit_reason) == ("terminated", "exited (137)")
    async with db.read() as conn:
        assert (await agents.get_agent(conn, agent_id)).state == "lost"
    with pytest.raises(UnauthorizedError):  # the instance's spare bootstrap is revoked
        async with db.write() as tx:
            await agents.register(tx, AgentRegister(bootstrap_token=token))
    assert inst.provider_ref not in target.containers  # exited container removed
    assert len(await instances(db)) == 1  # replacement


async def test_restart_during_termination_continues(
    db: Database, target: FakeTarget
) -> None:
    await make_pool(db, desired=1)
    first = TargetRegistry()
    first.override("docker", None, target)
    await tick(db, first)
    [inst] = await instances(db)
    async with db.write() as tx:
        await fleet.request_termination(tx, inst.id, "test")
    target.fail_stop = True
    await tick(db, first)
    # Argus restarts: fresh registry and reconciler, same database and provider.
    target.fail_stop = False
    second = TargetRegistry()
    second.override("docker", None, target)
    await tick(db, second)
    await tick(db, second)
    assert [i.state for i in await instances(db, True) if i.id == inst.id] == [
        "terminated"
    ]


# -- scaling down -----------------------------------------------------------------------


async def test_scale_down_drains_idle_and_spares_busy(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=2)
    await tick(db, registry)
    a, b = await instances(db)
    agent_a = await register_from(db, target, a.provider_ref)
    agent_b = await register_from(db, target, b.provider_ref)
    async with db.write() as tx:  # agent_b is busy
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        task = await tasks.claim(tx, agent_b, [])
    assert task is not None
    await scale(db, 1)
    await tick(db, registry)
    async with db.read() as conn:
        states = {
            i: (await agents.get_agent(conn, i)).state for i in (agent_a, agent_b)
        }
    assert states == {agent_a: "draining", agent_b: "active"}
    async with db.write() as tx:
        await agents.leave(tx, agent_a)  # the drained worker exits cleanly
    await tick(db, registry)  # all its agents left: stop the container
    await tick(db, registry)  # confirmed
    live = await instances(db)
    assert [i.id for i in live] == [b.id]
    async with db.write() as tx:  # busy work finished undisturbed
        await tasks.complete(tx, agent_b, task.id, task.attempt, DONE)


async def test_agent_never_registers(
    db: Database, registry: TargetRegistry, target: FakeTarget, clock: FakeClock
) -> None:
    await make_pool(db, desired=1)
    await tick(db, registry)
    clock.advance(301)
    await tick(db, registry)
    [inst] = await instances(db)
    assert (inst.state, inst.exit_reason) == (
        "terminating",
        "no agent registered in time",
    )


async def test_orphan_containers_are_stopped(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db, desired=0)
    target.containers[container_name("ghost")] = {
        "instance_id": "ghost",
        "pool": "web",
        "status": "running",
        "exit_code": None,
    }
    await tick(db, registry)
    assert target.stopped == [container_name("ghost")]


# -- the cap and the policy gate -----------------------------------------------------------


async def test_orchestrator_above_cap_needs_a_human(
    db: Database, registry: TargetRegistry
) -> None:
    await make_pool(db, desired=1, max_replicas=2)
    pool, decision_id = await scale(db, 2, Scope.ORCHESTRATOR)
    assert (pool.desired_replicas, decision_id) == (2, None)
    pool, decision_id = await scale(db, 6, Scope.ORCHESTRATOR)
    assert decision_id is not None
    assert (pool.desired_replicas, pool.max_replicas) == (2, 2)
    async with db.write() as tx:
        await decisions.answer(tx, decision_id, DecisionAnswer(choice="approve"), ADMIN)
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "web")
    assert (pool.desired_replicas, pool.max_replicas) == (6, 6)


def _boot(instance_id: str):
    from argus_core.agents import BootstrapTokenCreate

    return BootstrapTokenCreate(instance_id=instance_id)
