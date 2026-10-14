import asyncio

import pytest
from api_support import FakeClock
from argus_api import reaper
from argus_api.agents import service as agents
from argus_api.agents.service import LOST_AFTER_S
from argus_api.auth import authenticate as auth_authenticate
from argus_api.db import Database
from argus_api.errors import ConflictError, LeaseConflictError, UnauthorizedError
from argus_api.tasks import service as tasks
from argus_core.agents import AgentRegister, AgentState, BootstrapTokenCreate, Role
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S


async def bootstrap(db: Database, **kw) -> str:
    async with db.write() as tx:
        issued = await agents.create_bootstrap_token(
            tx, BootstrapTokenCreate(**kw), "human:test"
        )
    return issued.token


async def register(db: Database, caps: list[str] | None = None, **kw):
    token = await bootstrap(db, **kw)
    async with db.write() as tx:
        return await agents.register(
            tx, AgentRegister(bootstrap_token=token, capabilities=caps or [])
        )


async def authenticate(db: Database, token: str):
    async with db.read() as conn:
        return await auth_authenticate(conn, token)


async def reap(db: Database) -> reaper.ReapResult:
    async with db.write() as tx:
        return await reaper.reap(tx)


async def claim_one(db: Database, agent_id: str):
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(title="t"), "human:test")
        task = await tasks.claim(tx, agent_id, [])
    assert task is not None
    return task


async def heartbeat(db: Database, agent_id: str, leases=()):
    async with db.write() as tx:
        return await agents.heartbeat(tx, agent_id, list(leases))


# -- registration ------------------------------------------------------------------


async def test_register_returns_working_token(db: Database) -> None:
    reg = await register(db, caps=["python"], role=Role.ORCHESTRATOR)
    assert reg.agent.state is AgentState.ACTIVE
    assert reg.agent.role is Role.ORCHESTRATOR
    assert reg.agent.capabilities == ["python"]
    assert reg.token.startswith("argus_agent_")
    identity = await authenticate(db, reg.token)
    assert identity.agent_id == reg.agent.id


async def test_bootstrap_token_is_single_use(db: Database) -> None:
    token = await bootstrap(db)
    async with db.write() as tx:
        await agents.register(tx, AgentRegister(bootstrap_token=token))
    with pytest.raises(UnauthorizedError):
        async with db.write() as tx:
            await agents.register(tx, AgentRegister(bootstrap_token=token))


async def test_bootstrap_token_expires(db: Database, clock: FakeClock) -> None:
    token = await bootstrap(db, ttl_s=60)
    clock.advance(60)
    with pytest.raises(UnauthorizedError):
        async with db.write() as tx:
            await agents.register(tx, AgentRegister(bootstrap_token=token))


async def test_bootstrap_token_cannot_authenticate_requests(db: Database) -> None:
    with pytest.raises(UnauthorizedError):
        await authenticate(db, await bootstrap(db))


# -- heartbeat and leases ----------------------------------------------------------------


async def test_heartbeat_renews_listed_leases(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    clock.advance(100)  # past the 90 s loss threshold, but the heartbeat lands first
    hb = await heartbeat(db, reg.agent.id, [(task.id, task.attempt)])
    assert [r.task_id for r in hb.renewed] == [task.id]
    assert hb.rejected == []
    result = await reap(db)
    assert result.lost_agents == []
    assert result.expired_tasks == []


async def test_reaper_expires_overdue_lease_of_a_live_agent(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    clock.advance(task.lease_ttl_s)
    await heartbeat(db, reg.agent.id)  # alive, but did not renew this lease
    result = await reap(db)
    assert result.expired_tasks == [task.id]
    assert result.lost_agents == []


# -- loss -----------------------------------------------------------------------------


async def test_silent_agent_is_lost_and_everything_is_revoked(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db, instance_id="inst-1")
    spare_bootstrap = await bootstrap(db, instance_id="inst-1")
    other_instance_bootstrap = await bootstrap(db, instance_id="inst-2")
    task = await claim_one(db, reg.agent.id)

    clock.advance(LOST_AFTER_S - 1)
    assert (await reap(db)).lost_agents == []
    clock.advance(2)
    result = await reap(db)
    assert result.lost_agents == [reg.agent.id]

    async with db.read() as conn:
        agent = await agents.get_agent(conn, reg.agent.id)
        released = await tasks.get_task(conn, task.id)
    assert agent.state is AgentState.LOST
    assert agent.end_reason == "heartbeat timeout"
    assert released.state is S.READY
    assert released.failures == 1
    with pytest.raises(UnauthorizedError):
        await authenticate(db, reg.token)
    # The lost instance cannot register a replacement identity…
    with pytest.raises(UnauthorizedError):
        async with db.write() as tx:
            await agents.register(tx, AgentRegister(bootstrap_token=spare_bootstrap))
    # …while other instances are unaffected.
    async with db.write() as tx:
        await agents.register(
            tx, AgentRegister(bootstrap_token=other_instance_bootstrap)
        )


async def test_lost_is_irreversible(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    clock.advance(LOST_AFTER_S + 1)
    await reap(db)
    with pytest.raises(ConflictError) as err:
        await heartbeat(db, reg.agent.id, [(task.id, task.attempt)])
    assert err.value.code == "agent_lost"
    with pytest.raises(ConflictError):
        async with db.write() as tx:
            await tasks.claim(tx, reg.agent.id, [])


async def test_lost_agent_cannot_write_even_before_its_task_is_reassigned(
    db: Database,
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    async with db.write() as tx:  # simulate the loss transaction without expiry
        await tx.conn.execute(
            "UPDATE agents SET state = 'lost', ended_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000000Z", reg.agent.id),
        )
    with pytest.raises(LeaseConflictError) as err:
        async with db.write() as tx:
            await tasks.complete(tx, reg.agent.id, task.id, task.attempt, {})
    assert err.value.code == "agent_lost"


# -- draining and leaving -------------------------------------------------------------


async def test_draining_agent_finishes_work_but_claims_nothing_new(
    db: Database,
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    async with db.write() as tx:
        drained = await agents.drain(tx, reg.agent.id, "human:test")
    assert drained.state is AgentState.DRAINING
    assert drained.claimed_tasks == 1
    async with db.write() as tx:
        await tasks.complete(tx, reg.agent.id, task.id, task.attempt, {})
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await tasks.claim(tx, reg.agent.id, [])
    assert err.value.code == "agent_draining"


async def test_leave_ends_identity_and_releases_tasks(db: Database) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    async with db.write() as tx:
        left = await agents.leave(tx, reg.agent.id)
    assert left.state is AgentState.GONE
    assert left.claimed_tasks == 0
    async with db.read() as conn:
        assert (await tasks.get_task(conn, task.id)).state is S.READY
    with pytest.raises(UnauthorizedError):
        await authenticate(db, reg.token)


async def test_reaper_loop_runs_passes_and_survives_errors(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = await register(db)
    clock.advance(LOST_AFTER_S + 1)
    real_reap, calls = reaper.reap, []

    async def flaky_reap(tx):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return await real_reap(tx)

    monkeypatch.setattr(reaper, "reap", flaky_reap)
    loop = asyncio.create_task(reaper.run(db, interval=0.01))
    try:
        async with asyncio.timeout(2):
            while True:
                async with db.read() as conn:
                    if (
                        await agents.get_agent(conn, reg.agent.id)
                    ).state is AgentState.LOST:
                        break
                await asyncio.sleep(0.01)
    finally:
        loop.cancel()
    assert len(calls) >= 2
