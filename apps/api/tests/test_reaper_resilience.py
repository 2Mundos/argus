"""Reaper robustness and lease fencing (worker lifecycle 3.3, 3.4)."""

import json

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api import reaper
from argus_api.agents import service as agents
from argus_api.db import Database
from argus_api.errors import LeaseConflictError
from argus_api.reaper import reap
from argus_api.tasks import service as tasks
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S

A, B = "agent-a", "agent-b"


@pytest.fixture(autouse=True)
async def agent_rows(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="demo", title="t", **kw), "admin:test"
        )


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def run_reap(db: Database):
    async with db.write() as tx:
        return await reap(tx)


async def heartbeat(db: Database, agent: str) -> None:
    async with db.write() as tx:
        await agents.heartbeat(tx, agent, [])  # alive, but renews no lease


async def tick(db: Database, clock: FakeClock, seconds: int) -> None:
    """Let time pass with both agents alive (heartbeats renew no lease)."""
    for _ in range(seconds // 30):
        clock.advance(30)
        await heartbeat(db, A)
        await heartbeat(db, B)
    clock.advance(seconds % 30)
    await heartbeat(db, A)
    await heartbeat(db, B)


async def counts(db: Database, task_id: str) -> tuple[int, int]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM events WHERE subject_id = ?", (task_id,)
        ) as cur,
    ):
        events = (await cur.fetchone())[0]
    return events, (await get(db, task_id)).attempt


async def event_data(db: Database, task_id: str, kind: str) -> dict:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = ?"
            " ORDER BY rowid DESC",
            (task_id, kind),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return json.loads(row["data"])


# -- expiry is recorded, not silent ----------------------------------------------


async def test_a_live_agent_with_an_expired_lease_is_expired_and_requeued(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, max_attempts=3)
    claimed = await claim(db)
    await tick(db, clock, 150)  # A stays alive but never renews the lease
    result = await run_reap(db)
    assert result.expired_tasks == [task.id]
    assert result.lost_agents == []
    after = await get(db, task.id)
    assert (after.state, after.assignee_agent_id, after.failures) == (S.READY, None, 1)
    assert after.attempt == claimed.attempt


async def test_expiry_records_a_structured_reason(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, max_attempts=3)
    await claim(db)
    await tick(db, clock, 121)
    await run_reap(db)
    after = await get(db, task.id)
    assert after.error is not None
    assert after.error.startswith("lease expired at ")
    assert f"holder {A}" in after.error
    assert after.progress_message == "attempt 1 ended: lease expired; requeued"
    data = await event_data(db, task.id, "task.expire")
    assert data["reason"] == "lease_expired"
    assert data["error"] == after.error


async def test_timeout_and_agent_loss_record_their_reasons(
    db: Database, clock: FakeClock
) -> None:
    slow = await create(db, timeout_s=60, max_attempts=1)
    await claim(db)
    clock.advance(61)
    async with db.write() as tx:  # the lease is still valid: only the deadline passed
        await tasks.renew(tx, A, [(slow.id, 1)])
    await run_reap(db)
    after = await get(db, slow.id)
    assert after.state is S.FAILED
    assert (after.error or "").startswith("attempt timed out at ")
    assert after.progress_message == "attempt 1 ended: timeout; failed"

    lost = await create(db, max_attempts=3)
    await claim(db, B)
    async with db.write() as tx:
        await agents.leave(tx, B)
    after = await get(db, lost.id)
    assert after.state is S.READY
    assert f"holder {B} ended" in (after.error or "")
    assert after.progress_message == "attempt 1 ended: agent ended; requeued"


# -- one bad stage or row never blocks the rest ----------------------------------------


async def test_a_failing_stage_does_not_roll_back_lease_expiry(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await create(db)
    await claim(db)
    await tick(db, clock, 121)

    async def boom(tx):
        raise RuntimeError("accounts exploded")

    monkeypatch.setattr(reaper.accounts, "recover", boom)
    result = await run_reap(db)
    assert result.expired_tasks == [task.id]
    assert result.failed_stages == ["accounts.recover"]
    assert (await get(db, task.id)).state is S.READY


async def test_a_failing_row_does_not_block_other_rows(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [(await create(db)).id for _ in range(3)]
    for _ in ids:
        await claim(db, A)
    await tick(db, clock, 121)
    bad = sorted(ids)[0]
    real = tasks._apply

    async def flaky(tx, row, *args, **kw):
        if row["id"] == bad:
            await tx.conn.execute(
                "UPDATE tasks SET title = 'half-written' WHERE id=?", (bad,)
            )
            raise RuntimeError("bad row")
        return await real(tx, row, *args, **kw)

    monkeypatch.setattr(tasks, "_apply", flaky)
    result = await run_reap(db)
    assert sorted(result.expired_tasks) == sorted(
        ids[1:] if ids[0] == bad else set(ids) - {bad}
    )
    assert result.failed_tasks == [bad]
    assert (await get(db, bad)).state is S.CLAIMED
    assert (await get(db, bad)).title == "t"  # the row's partial write was rolled back
    for other in set(ids) - {bad}:
        assert (await get(db, other)).state is S.READY
    monkeypatch.undo()
    assert (await run_reap(db)).expired_tasks == [bad]  # retried next pass


async def test_events_of_a_rolled_back_row_are_not_published(
    db: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await create(db)
    await claim(db)
    await tick(db, clock, 121)
    real = tasks._apply

    async def record_then_fail(tx, row, *args, **kw):
        await real(tx, row, *args, **kw)
        raise RuntimeError("after the event")

    monkeypatch.setattr(tasks, "_apply", record_then_fail)
    async with db.write() as tx:
        await reap(tx)
        assert [e for e in tx.events if e.kind == "task.expire"] == []
    assert (await get(db, task.id)).state is S.CLAIMED


# -- fencing: a valid lease is not stolen; a stale holder cannot write ----------------


async def test_a_valid_lease_is_not_stolen(db: Database, clock: FakeClock) -> None:
    task = await create(db)
    await claim(db)
    clock.advance(60)  # half of the 120 s lease
    assert (await run_reap(db)).expired_tasks == []
    after = await get(db, task.id)
    assert (after.state, after.assignee_agent_id) == (S.CLAIMED, A)


async def test_a_stale_holder_cannot_touch_a_task_another_agent_now_holds(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, max_attempts=3)
    first = await claim(db, A)
    await tick(db, clock, 121)
    await run_reap(db)
    second = await claim(db, B)
    assert (second.attempt, second.assignee_agent_id) == (2, B)
    before = await get(db, task.id)
    events_before = await counts(db, task.id)

    async def late(call):
        with pytest.raises(LeaseConflictError) as err:
            async with db.write() as tx:
                await call(tx)
        return err.value.code

    a = first.attempt
    assert (
        await late(lambda tx: tasks.fail(tx, A, task.id, a, "late")) == "not_assignee"
    )
    assert (
        await late(lambda tx: tasks.complete(tx, A, task.id, a, DONE)) == "not_assignee"
    )
    assert (
        await late(lambda tx: tasks.report_progress(tx, A, task.id, a, "x"))
        == "not_assignee"
    )
    async with db.write() as tx:
        renewed, rejected = await tasks.renew(tx, A, [(task.id, a)])
    assert renewed == [] and rejected[0].code == "not_assignee"
    # even the same agent id on the stale attempt number is fenced
    assert (
        await late(lambda tx: tasks.fail(tx, B, task.id, a, "late")) == "stale_attempt"
    )

    assert await get(db, task.id) == before
    assert await counts(db, task.id) == events_before


async def test_history_survives_expiry_and_requeue(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, max_attempts=3)
    await claim(db, A)
    before_events, _ = await counts(db, task.id)
    await tick(db, clock, 121)
    await run_reap(db)
    events, attempt = await counts(db, task.id)
    assert events == before_events + 1  # only the expiry was appended
    assert attempt == 1
    await claim(db, B)
    events2, attempt2 = await counts(db, task.id)
    assert (events2, attempt2) == (events + 1, 2)


# -- the worker's own FAIL is not lost to a lapsed lease -------------------------------


async def test_fail_records_reason_and_text(db: Database) -> None:
    task = await create(db, max_attempts=1)
    claimed = await claim(db)
    async with db.write() as tx:
        await tasks.fail(tx, A, task.id, claimed.attempt, "cannot open the PR")
    after = await get(db, task.id)
    assert (after.state, after.error) == (S.FAILED, "cannot open the PR")
    data = await event_data(db, task.id, "task.fail")
    assert data["reason"] == "worker_fail"
    assert data["error"] == "cannot open the PR"


async def test_a_fail_just_after_the_lease_lapsed_is_accepted(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, max_attempts=1)
    claimed = await claim(db)
    clock.advance(121)  # lapsed, reaper has not run yet
    async with db.write() as tx:
        after = await tasks.fail(tx, A, task.id, claimed.attempt, "FAIL text kept")
    assert (after.state, after.error) == (S.FAILED, "FAIL text kept")


async def test_other_worker_writes_still_need_a_valid_lease(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db)
    claimed = await claim(db)
    await tick(db, clock, 121)
    with pytest.raises(LeaseConflictError) as err:
        async with db.write() as tx:
            await tasks.complete(tx, A, task.id, claimed.attempt, DONE)
    assert err.value.code == "lease_expired"


async def test_a_late_fail_after_requeue_is_refused(
    db: Database, clock: FakeClock
) -> None:
    task = await create(db, max_attempts=3)
    claimed = await claim(db)
    await tick(db, clock, 121)
    await run_reap(db)
    with pytest.raises(LeaseConflictError) as err:
        async with db.write() as tx:
            await tasks.fail(tx, A, task.id, claimed.attempt, "too late")
    assert err.value.code == "not_claimed"
    assert (await get(db, task.id)).state is S.READY
