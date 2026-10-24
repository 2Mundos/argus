"""Quiet claims, activity_at, explained late expiry (stall-visibility.spec.md)."""

import json
from datetime import timedelta

from api_support import FakeClock
from argus_api import reaper
from argus_api.agents import service as agents
from argus_api.agents.service import LOST_AFTER_S
from argus_api.db import Database, ts
from argus_api.fleet_health import classify_failure
from argus_api.inbox import inbox
from argus_api.tasks import service as tasks
from argus_core.agents import AgentRegister, BootstrapTokenCreate
from argus_core.tasks import TaskCreate


async def bootstrap(db: Database, **kw) -> str:
    async with db.write() as tx:
        issued = await agents.create_bootstrap_token(
            tx, BootstrapTokenCreate(**kw), "human:test"
        )
    return issued.token


async def register(db: Database, **kw):
    token = await bootstrap(db, **kw)
    async with db.write() as tx:
        return await agents.register(tx, AgentRegister(bootstrap_token=token))


async def claim_one(db: Database, agent_id: str):
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="quiet-me"), "human:test"
        )
        task = await tasks.claim(tx, agent_id, [])
    assert task is not None
    return task


async def reap(
    db: Database, *, quiet_claim_s: int = 1200, stall_fail_s: int = 0
) -> reaper.ReapResult:
    async with db.write() as tx:
        return await reaper.reap(
            tx, quiet_claim_s=quiet_claim_s, stall_fail_s=stall_fail_s
        )


async def hold_alive(
    db: Database,
    clock: FakeClock,
    agent_id: str,
    leases: list,
    seconds: float,
) -> None:
    """Advance time while heartbeating so the agent and leases stay live."""
    left = seconds
    while left > 0:
        step = min(left, LOST_AFTER_S - 1, 60)
        clock.advance(step)
        async with db.write() as tx:
            await agents.heartbeat(tx, agent_id, leases)
        left -= step


async def test_flag_quiet_after_threshold(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    leases = [(task.id, task.attempt)]
    await hold_alive(db, clock, reg.agent.id, leases, 1199)
    assert (await reap(db, quiet_claim_s=1200)).quieted_tasks == []
    await hold_alive(db, clock, reg.agent.id, leases, 2)
    result = await reap(db, quiet_claim_s=1200)
    assert result.quieted_tasks == [task.id]
    async with db.read() as conn:
        async with conn.execute(
            "SELECT quiet_at FROM tasks WHERE id = ?", (task.id,)
        ) as cur:
            assert (await cur.fetchone())["quiet_at"] is not None
        async with conn.execute(
            "SELECT kind FROM events WHERE subject_id = ? AND kind = 'task.quiet'",
            (task.id,),
        ) as cur:
            assert await cur.fetchone() is not None
    assert (await reap(db, quiet_claim_s=1200)).quieted_tasks == []


async def test_stalled_claim_is_expired_after_it_stays_silent(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    leases = [(task.id, task.attempt)]
    await hold_alive(db, clock, reg.agent.id, leases, 1300)
    assert (await reap(db, stall_fail_s=3600)).quieted_tasks == [task.id]
    await hold_alive(db, clock, reg.agent.id, leases, 2000)  # 3300 s silent
    assert (await reap(db, stall_fail_s=3600)).expired_tasks == []
    await hold_alive(db, clock, reg.agent.id, leases, 400)  # 3700 s silent
    assert (await reap(db, stall_fail_s=3600)).expired_tasks == [task.id]
    async with db.read() as conn:
        after = await tasks.get_task(conn, task.id)
        async with conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.expire'",
            (task.id,),
        ) as cur:
            data = json.loads((await cur.fetchone())["data"])
    assert (after.state, after.failures) == ("ready", 1)
    assert data["reason"] == "stalled" and "stalled" in data["error"]
    assert data["last_signal_source"] == "claim"


async def test_a_signal_or_a_zero_setting_keeps_a_silent_claim(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    leases = [(task.id, task.attempt)]
    await hold_alive(db, clock, reg.agent.id, leases, 1300)
    await reap(db, stall_fail_s=3600)  # flags it quiet
    await hold_alive(db, clock, reg.agent.id, leases, 4000)
    assert (await reap(db, stall_fail_s=0)).expired_tasks == []  # disabled
    async with db.write() as tx:  # a fresh signal arrives just before the reap
        await agents.heartbeat(tx, reg.agent.id, [(task.id, task.attempt, clock.now)])
    assert (await reap(db, stall_fail_s=3600)).expired_tasks == []


async def test_activity_at_clears_quiet(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    leases = [(task.id, task.attempt)]
    await hold_alive(db, clock, reg.agent.id, leases, 1300)
    assert (await reap(db, quiet_claim_s=1200)).quieted_tasks == [task.id]
    at = clock.now
    async with db.write() as tx:
        await agents.heartbeat(tx, reg.agent.id, [(task.id, task.attempt, at)])
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT quiet_at, activity_at FROM tasks WHERE id = ?", (task.id,)
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row["quiet_at"] is None
    assert row["activity_at"] is not None


async def test_activity_at_clamped_and_never_moves_backwards(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    now = clock.now
    future = now + timedelta(hours=1)
    past = now - timedelta(minutes=5)
    async with db.write() as tx:
        await agents.heartbeat(tx, reg.agent.id, [(task.id, task.attempt, future)])
    async with (
        db.read() as conn,
        conn.execute("SELECT activity_at FROM tasks WHERE id = ?", (task.id,)) as cur,
    ):
        first = (await cur.fetchone())["activity_at"]
    assert first == ts(now)  # clamped to tx.now
    clock.advance(10)
    async with db.write() as tx:
        await agents.heartbeat(tx, reg.agent.id, [(task.id, task.attempt, past)])
    async with (
        db.read() as conn,
        conn.execute("SELECT activity_at FROM tasks WHERE id = ?", (task.id,)) as cur,
    ):
        second = (await cur.fetchone())["activity_at"]
    assert second == first  # never moves backwards


async def test_progress_clears_quiet(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    leases = [(task.id, task.attempt)]
    await hold_alive(db, clock, reg.agent.id, leases, 1300)
    await reap(db, quiet_claim_s=1200)
    async with db.write() as tx:
        await tasks.report_progress(
            tx, reg.agent.id, task.id, task.attempt, "still working"
        )
    async with (
        db.read() as conn,
        conn.execute("SELECT quiet_at FROM tasks WHERE id = ?", (task.id,)) as cur,
    ):
        assert (await cur.fetchone())["quiet_at"] is None


async def test_inbox_last_signal_uses_newest_not_coalesce(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    claim_time = clock.now
    clock.advance(60)
    progress_time = clock.now
    async with db.write() as tx:
        await tasks.report_progress(tx, reg.agent.id, task.id, task.attempt, "working")
        await agents.heartbeat(
            tx,
            reg.agent.id,
            [(task.id, task.attempt, claim_time)],
        )
    async with db.write() as tx:
        await tasks.flag_quiet(tx, quiet_after_s=0, failed=None)
    async with db.snapshot() as snap:
        page = await inbox(snap)
    row = next(i for i in page.items if i.subject_id == task.id)
    assert row.last_signal_source == "progress"
    assert row.last_signal_at == progress_time


async def test_inbox_lists_quiet_claims(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    leases = [(task.id, task.attempt)]
    await hold_alive(db, clock, reg.agent.id, leases, 1300)
    await reap(db, quiet_claim_s=1200)
    async with db.snapshot() as snap:
        page = await inbox(snap)
    rows = [i for i in page.items if i.subject_id == task.id]
    assert len(rows) == 1
    assert rows[0].state == "claimed"
    assert rows[0].last_signal_at is not None
    assert page.totals_by_state.get("claimed") == 1


async def test_agent_quiet_since(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    leases = [(task.id, task.attempt)]
    await hold_alive(db, clock, reg.agent.id, leases, 1300)
    await reap(db, quiet_claim_s=1200)
    async with db.read() as conn:
        agent = await agents.get_agent(conn, reg.agent.id)
    assert agent.quiet_since is not None
    assert agent.claimed_tasks == 1


async def test_expiry_includes_last_signal_and_detected_late(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    # Keep the agent alive but do not renew the lease → lease expires late.
    left = task.lease_ttl_s + 120
    while left > 0:
        step = min(left, LOST_AFTER_S - 1)
        clock.advance(step)
        async with db.write() as tx:
            await agents.heartbeat(tx, reg.agent.id, [])  # no lease renewal
        left -= step
    result = await reap(db)
    assert task.id in result.expired_tasks
    async with db.read() as conn:
        async with conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.expire'"
            " ORDER BY id DESC LIMIT 1",
            (task.id,),
        ) as cur:
            payload = json.loads((await cur.fetchone())["data"])
        row = await tasks.get_task(conn, task.id)
    assert payload["reason"] == "lease_expired"
    assert payload.get("detected_late_s", 0) > 30
    assert "last signal" in (row.error or "")
    assert "detected" in (row.error or "") and "late" in (row.error or "")


async def test_lost_agent_records_detected_late(db: Database, clock: FakeClock) -> None:
    reg = await register(db)
    clock.advance(LOST_AFTER_S + 600)  # ten minutes late
    result = await reap(db)
    assert result.lost_agents == [reg.agent.id]
    async with db.read() as conn:
        agent = await agents.get_agent(conn, reg.agent.id)
        async with conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'agent.lost'",
            (reg.agent.id,),
        ) as cur:
            data = json.loads((await cur.fetchone())["data"])
    assert "detected" in (agent.end_reason or "")
    assert data.get("detected_late_s", 0) > 30


def test_classify_session_lost() -> None:
    assert classify_failure("session_lost", "container gone") == "session_lost"
