import json
from datetime import timedelta

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.agents import service as agents
from argus_api.db import Database
from argus_api.errors import LeaseConflictError
from argus_api.kinds import service as kinds
from argus_api.reaper import reap
from argus_api.tasks import service as tasks
from argus_core.kinds import TaskKindUpsert
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S

A = "agent-a"


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="global", title="t", **kw), "admin:test"
        )


async def claim(db: Database, default: int = 0):
    async with db.write() as tx:
        task = await tasks.claim(tx, A, [], default_timeout_s=default)
    assert task is not None
    return task


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def reaped(db: Database) -> list[str]:
    async with db.write() as tx:
        return (await reap(tx)).expired_tasks


async def test_deadline_comes_from_task_then_kind_then_server(
    db: Database, clock: FakeClock
) -> None:
    async with db.write() as tx:
        await kinds.upsert_kind(
            tx, "slow", TaskKindUpsert(default_timeout_s=7200), "admin:test"
        )
    await create(db, timeout_s=600)  # explicit
    await create(db, kind="slow")  # from the kind
    await create(db)  # from the server default
    seen = []
    for _ in range(3):
        t = await claim(db, default=3600)
        seen.append((t.timeout_at - clock.now).total_seconds())  # ty: ignore[unsupported-operator]
    assert sorted(seen) == [600, 3600, 7200]


async def test_zero_disables_the_default(db: Database) -> None:
    await create(db)
    assert (await claim(db, default=0)).timeout_at is None


async def test_a_healthy_holder_is_still_cut_off(
    db: Database, clock: FakeClock
) -> None:
    """The point of the feature: heartbeats and progress renew the lease forever,
    but not the attempt's wall-clock deadline."""
    task = await create(db, timeout_s=600, max_attempts=3)
    claimed = await claim(db)
    for _ in range(9):  # 9 x 60 s, alive and chatty the whole time
        clock.advance(60)
        async with db.write() as tx:
            await agents.heartbeat(tx, A, [(task.id, claimed.attempt)])
            await tasks.report_progress(tx, A, task.id, claimed.attempt, "still on it")
        assert await reaped(db) == []
    clock.advance(60)  # 600 s since the claim
    async with db.write() as tx:
        await agents.heartbeat(tx, A, [(task.id, claimed.attempt)])  # lease is fine…
    assert await reaped(db) == [task.id]  # …the deadline is not
    after = await get(db, task.id)
    assert (after.state, after.failures, after.assignee_agent_id) == (S.READY, 1, None)
    assert after.timeout_at is None

    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.expire'",
            (task.id,),
        ) as cur,
    ):
        assert json.loads((await cur.fetchone())["data"])["reason"] == "timeout"


async def test_the_holder_is_told_to_stop(db: Database, clock: FakeClock) -> None:
    task = await create(db, timeout_s=60)
    claimed = await claim(db)
    clock.advance(61)
    await reaped(db)
    with pytest.raises(LeaseConflictError):
        async with db.write() as tx:
            await tasks.complete(tx, A, task.id, claimed.attempt, DONE)


async def test_timeouts_are_failures_and_exhaust_attempts(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, timeout_s=60, max_attempts=2)
    for expected in (S.READY, S.FAILED):
        await claim(db)
        clock.advance(61)
        await reaped(db)
        assert (await get(db, task.id)).state is expected


async def test_a_fresh_attempt_gets_a_fresh_deadline(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, timeout_s=300)
    first = await claim(db)
    clock.advance(100)  # within its 120 s lease
    async with db.write() as tx:  # it fails voluntarily; goes back to ready
        await tasks.fail(tx, A, task.id, first.attempt, "oops")
    second = await claim(db)
    assert second.timeout_at == clock.now + timedelta(seconds=300)
