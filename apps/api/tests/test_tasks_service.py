import asyncio
import json
from datetime import timedelta

import pytest
from api_support import FakeClock, insert_agent
from argus_api.db import Database
from argus_api.errors import LeaseConflictError, NotFoundError
from argus_api.tasks import service
from argus_core.tasks import InvalidTransitionError, Review, Task, TaskCreate
from argus_core.tasks import TaskState as S

A, B = "agent-a", "agent-b"


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)


async def create(db: Database, **kw) -> Task:
    async with db.write() as tx:
        return await service.create_task(tx, TaskCreate(title="t", **kw), "human:test")


async def claim(db: Database, agent: str = A, caps: list[str] | None = None):
    async with db.write() as tx:
        return await service.claim(tx, agent, caps or [])


async def get(db: Database, task_id: str) -> Task:
    async with db.read() as conn:
        return await service.get_task(conn, task_id)


async def events(db: Database, task_id: str) -> list[tuple[str, dict]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT kind, data FROM events WHERE subject_id = ? ORDER BY id", (task_id,)
        ) as cur,
    ):
        return [(r["kind"], json.loads(r["data"])) for r in await cur.fetchall()]


# -- creation and dependencies ---------------------------------------------------


async def test_new_task_without_deps_is_ready(db: Database) -> None:
    task = await create(db)
    assert task.state is S.READY
    assert await events(db, task.id) == [("task.created", {"to": "ready"})]


async def test_task_with_unfinished_dep_is_pending_until_dep_succeeds(
    db: Database,
) -> None:
    dep = await create(db)
    task = await create(db, depends_on=[dep.id])
    assert task.state is S.PENDING
    assert task.depends_on == [dep.id]

    claimed = await claim(db)
    assert claimed is not None
    assert claimed.id == dep.id
    async with db.write() as tx:
        await service.complete(tx, A, dep.id, claimed.attempt, {})
    assert (await get(db, task.id)).state is S.READY


async def test_dependents_wait_for_all_deps(db: Database) -> None:
    d1, d2 = await create(db), await create(db)
    task = await create(db, depends_on=[d1.id, d2.id])
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        await service.complete(tx, A, c.id, c.attempt, {})
    assert (await get(db, task.id)).state is S.PENDING


async def test_unknown_dependency_or_parent_is_rejected(db: Database) -> None:
    with pytest.raises(NotFoundError):
        await create(db, depends_on=["nope"])
    with pytest.raises(NotFoundError):
        await create(db, parent_id="nope")


async def test_failed_dependency_leaves_dependent_pending_with_event(
    db: Database,
) -> None:
    dep = await create(db, max_attempts=1)
    task = await create(db, depends_on=[dep.id])
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        await service.fail(tx, A, dep.id, c.attempt, "broken")
    assert (await get(db, dep.id)).state is S.FAILED
    assert (await get(db, task.id)).state is S.PENDING
    assert (await events(db, task.id))[-1] == (
        "task.dependency_failed",
        {"dependency_id": dep.id, "dependency_state": "failed"},
    )


# -- claiming --------------------------------------------------------------------


async def test_claim_picks_highest_priority_then_oldest(
    db: Database, clock: FakeClock
) -> None:
    old_low = await create(db, priority=0)
    clock.advance(1)
    high = await create(db, priority=5)
    clock.advance(1)
    await create(db, priority=0)
    assert (c := await claim(db)) is not None
    assert c.id == high.id
    assert (c := await claim(db)) is not None
    assert c.id == old_low.id


async def test_claim_requires_all_capabilities(db: Database) -> None:
    task = await create(db, required_capabilities=["python", "git"])
    assert await claim(db, caps=["python"]) is None
    c = await claim(db, caps=["git", "python", "docker"])
    assert c is not None
    assert c.id == task.id


async def test_claim_sets_lease_and_attempt(db: Database, clock: FakeClock) -> None:
    await create(db, lease_ttl_s=60)
    c = await claim(db)
    assert c is not None
    assert c.state is S.CLAIMED
    assert c.assignee_agent_id == A
    assert c.attempt == 1
    assert c.lease_expires_at == clock.now + timedelta(seconds=60)
    assert await claim(db, B) is None


# -- ownership and fencing (3.3) -------------------------------------------------


async def test_write_after_expiry_before_reaping_is_rejected(
    db: Database, clock: FakeClock
) -> None:
    await create(db, lease_ttl_s=60)
    c = await claim(db)
    assert c is not None
    clock.advance(60)  # deadline reached; the reaper has not run
    with pytest.raises(LeaseConflictError) as err:
        async with db.write() as tx:
            await service.complete(tx, A, c.id, c.attempt, {})
    assert err.value.code == "lease_expired"
    assert (await get(db, c.id)).state is S.CLAIMED  # unchanged


async def test_stale_attempt_after_reassignment_is_rejected(
    db: Database, clock: FakeClock
) -> None:
    await create(db, lease_ttl_s=60)
    first = await claim(db, A)
    assert first is not None
    clock.advance(61)
    async with db.write() as tx:
        assert await service.expire_leases(tx) == [first.id]
    second = await claim(db, A)  # same agent, new attempt
    assert second is not None
    assert second.attempt == first.attempt + 1
    with pytest.raises(LeaseConflictError) as err:
        async with db.write() as tx:
            await service.complete(tx, A, first.id, first.attempt, {})
    assert err.value.code == "stale_attempt"


async def test_other_agent_cannot_write(db: Database) -> None:
    await create(db)
    c = await claim(db, A)
    assert c is not None
    with pytest.raises(LeaseConflictError) as err:
        async with db.write() as tx:
            await service.complete(tx, B, c.id, c.attempt, {})
    assert err.value.code == "not_assignee"


async def test_expiry_while_waiting_for_the_writer(
    db: Database, clock: FakeClock
) -> None:
    await create(db, lease_ttl_s=60)
    c = await claim(db)
    assert c is not None
    clock.advance(59)  # still valid when the worker sends its write

    async def worker_write() -> None:
        async with db.write() as tx:
            await service.complete(tx, A, c.id, c.attempt, {})

    async with db.write():  # someone else holds the writer…
        pending = asyncio.create_task(worker_write())
        await asyncio.sleep(0)
        clock.advance(2)  # …until after the deadline
    with pytest.raises(LeaseConflictError) as err:
        await pending
    assert err.value.code == "lease_expired"


async def test_renewal_extends_valid_leases_and_rejects_others(
    db: Database, clock: FakeClock
) -> None:
    await create(db, lease_ttl_s=60)
    await create(db, lease_ttl_s=60)
    t1, t2 = await claim(db), await claim(db)
    assert t1 is not None
    assert t2 is not None
    clock.advance(30)
    async with db.write() as tx:
        renewed, rejected = await service.renew(
            tx, A, [(t1.id, t1.attempt), (t2.id, 99), ("missing", 1)]
        )
    assert [(r.task_id, r.lease_expires_at) for r in renewed] == [
        (t1.id, clock.now + timedelta(seconds=60))
    ]
    assert [(r.task_id, r.code) for r in rejected] == [
        (t2.id, "stale_attempt"),
        ("missing", "not_found"),
    ]


async def test_renewal_cannot_revive_an_expired_lease(
    db: Database, clock: FakeClock
) -> None:
    await create(db, lease_ttl_s=60)
    c = await claim(db)
    assert c is not None
    clock.advance(60)
    async with db.write() as tx:
        renewed, rejected = await service.renew(tx, A, [(c.id, c.attempt)])
    assert renewed == []
    assert rejected[0].code == "lease_expired"


# -- failure, expiry, retries ------------------------------------------------------


async def test_failures_retry_until_max_attempts(db: Database) -> None:
    task = await create(db, max_attempts=2)
    for expected in (S.READY, S.FAILED):
        c = await claim(db)
        assert c is not None
        async with db.write() as tx:
            after = await service.fail(tx, A, task.id, c.attempt, "nope")
        assert after.state is expected
        assert after.assignee_agent_id is None
        assert after.lease_expires_at is None
    assert after.failures == 2
    assert after.error == "nope"


async def test_expire_leases_counts_as_failure(db: Database, clock: FakeClock) -> None:
    task = await create(db, lease_ttl_s=60, max_attempts=1)
    await claim(db)
    clock.advance(59)
    async with db.write() as tx:
        assert await service.expire_leases(tx) == []
    clock.advance(1)
    async with db.write() as tx:
        assert await service.expire_leases(tx) == [task.id]
    assert (await get(db, task.id)).state is S.FAILED


# -- block, cancel, approval, review --------------------------------------------------


async def test_block_releases_lease_and_unblock_requeues(db: Database) -> None:
    task = await create(db)
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        blocked = await service.block(tx, A, task.id, c.attempt, "need creds")
    assert blocked.state is S.BLOCKED
    assert blocked.block_reason == "need creds"
    assert blocked.assignee_agent_id is None
    async with db.write() as tx:
        ready = await service.unblock(tx, task.id, "human:test")
    assert ready.state is S.READY
    assert ready.failures == 0


async def test_cancel_terminal_task_is_invalid(db: Database) -> None:
    task = await create(db)
    async with db.write() as tx:
        await service.cancel(tx, task.id, "human:test")
    with pytest.raises(InvalidTransitionError):
        async with db.write() as tx:
            await service.cancel(tx, task.id, "human:test")


async def test_approval_gate_respects_deps(db: Database) -> None:
    dep = await create(db)
    task = await create(db, requires_approval=True, depends_on=[dep.id])
    assert task.state is S.AWAITING_APPROVAL
    async with db.write() as tx:
        assert (await service.approve(tx, task.id, "human:test")).state is S.PENDING


async def test_review_reject_requeues_without_counting_failure(db: Database) -> None:
    task = await create(db, review=Review.HUMAN, max_attempts=1, spec="do it")
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        done = await service.complete(tx, A, task.id, c.attempt, {"ok": True})
    assert done.state is S.IN_REVIEW
    assert done.result == {"ok": True}
    async with db.write() as tx:
        rejected = await service.review(
            tx, task.id, "human:test", accept=False, notes="add tests"
        )
    assert rejected.state is S.READY
    assert rejected.failures == 0
    assert "add tests" in rejected.spec
    # Attempt keeps increasing for fencing even though failures did not.
    c2 = await claim(db)
    assert c2 is not None
    assert c2.attempt == 2
    async with db.write() as tx:
        await service.complete(tx, A, task.id, c2.attempt, {})
    async with db.write() as tx:
        accepted = await service.review(tx, task.id, "human:test", accept=True)
    assert accepted.state is S.SUCCEEDED


# -- events and the bus ----------------------------------------------------------------


async def test_every_transition_writes_one_event(db: Database) -> None:
    task = await create(db)
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        await service.complete(tx, A, task.id, c.attempt, {})
    assert [k for k, _ in await events(db, task.id)] == [
        "task.created",
        "task.claim",
        "task.complete",
    ]


async def test_events_are_published_only_after_commit(db: Database) -> None:
    with db.bus.subscribe() as sub:
        with pytest.raises(RuntimeError):
            async with db.write() as tx:
                await service.create_task(tx, TaskCreate(title="x"), "human:test")
                raise RuntimeError("rollback")
        await create(db)
        async with asyncio.timeout(1):
            event = await sub.next_matching(lambda e: True)
    assert event.kind == "task.created"
    assert (await get(db, event.subject_id)).title == "t"


async def test_claimed_state_requires_assignee_at_the_database_level(
    db: Database,
) -> None:
    task = await create(db)
    with pytest.raises(Exception, match="CHECK constraint failed"):
        async with db.write() as tx:
            await tx.conn.execute(
                "UPDATE tasks SET state = 'claimed' WHERE id = ?", (task.id,)
            )
