"""The blockers that stalled reviews and pauses (failure classes 3.2.2, 3.6)."""

import pytest
from api_support import insert_agent
from argus_api.db import Database, ts
from argus_api.fleet import service as fleet
from argus_api.tasks import service
from argus_core.fleet import PoolUpsert
from argus_core.tasks import RETRY_BUDGET, RetryClass, Task, TaskCreate
from argus_core.tasks import TaskState as S

A = "agent-a"


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)


async def create(db: Database, **kw) -> Task:
    async with db.write() as tx:
        return await service.create_task(
            tx, TaskCreate(project="demo", title="t", **kw), "human:test"
        )


async def claim(db: Database):
    async with db.write() as tx:
        return await service.claim(tx, A, [])


async def block_once(db: Database, task_id: str, reason: str) -> Task:
    c = await claim(db)
    assert c is not None and c.id == task_id
    async with db.write() as tx:
        return await service.block(tx, A, task_id, c.attempt, reason, "boom")


@pytest.mark.parametrize("reason", ["worker_env", "env_workspace_prior_unverified"])
async def test_an_environment_block_loop_ends_failed_and_stays_failed(
    db: Database, reason: str
) -> None:
    budget = RETRY_BUDGET[RetryClass.ENVIRONMENT]
    task = await create(db, max_attempts=2)
    for _ in range(budget):
        after = await block_once(db, task.id, reason)
        assert (after.state, after.block_reason) == (S.BLOCKED, reason)
        assert after.failures == 0
        async with db.write() as tx:
            await service.unblock(tx, task.id, "human:test")
    # attempt budget + 1 > max_attempts, yet nothing was counted as a failure
    after = await block_once(db, task.id, reason)
    assert after.state is S.FAILED
    assert reason in (after.error or "")
    assert await claim(db) is None  # a failed task never re-enters ready by itself
    async with db.write() as tx:
        assert (await service.retry(tx, task.id, "human:test")).state is S.READY
    after = await block_once(db, task.id, reason)  # an operator retry resets the budget
    assert after.state is S.BLOCKED


async def test_quota_and_expired_login_spend_no_attempt(db: Database) -> None:
    task = await create(db, max_attempts=1)
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        after = await service.fail(
            tx, A, task.id, c.attempt, "HTTP 401 token_expired", "provider_failure"
        )
    assert (after.state, after.block_reason, after.failures) == (
        S.BLOCKED,
        "env_provider_auth",
        0,
    )
    async with db.write() as tx:
        await service.unblock(tx, task.id, "human:test")
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        after = await service.pause_for_quota(
            tx, A, task.id, c.attempt, "usage limit reached", wait=True
        )
    assert (after.state, after.block_reason, after.failures) == (
        S.BLOCKED,
        "awaiting_quota",
        0,
    )


async def test_a_worker_fail_with_quota_words_is_still_the_workers_word(
    db: Database,
) -> None:
    """Probe: the model's own text never classifies (failure classes 3.6), so a FAIL
    that merely quotes a quota notice blocks as `worker_gave_up` and counts."""
    task = await create(db, max_attempts=3)
    c = await claim(db)
    assert c is not None
    async with db.write() as tx:
        after = await service.fail(
            tx, A, task.id, c.attempt, "You've hit your limit · resets 2am (UTC)"
        )
    assert (after.state, after.block_reason, after.failures) == (
        S.BLOCKED,
        "worker_gave_up",
        1,
    )


async def test_an_account_whose_capability_is_on_no_live_pool_is_reported(
    db: Database,
):
    """The account is available and leasable, but no wanted or running pool
    carries a capability of its provider: inspect_pool names what is missing."""
    async with db.write() as tx:
        for i, (name, provider) in enumerate(
            [("work-claude", "anthropic"), ("work-codex", "openai")]
        ):
            await tx.conn.execute(
                "INSERT INTO accounts (id, name, provider, kind, secret_ref, state,"
                " created_at, updated_at) VALUES (?, ?, ?, 'subscription', 'ref',"
                " 'available', ?, ?)",
                (f"acc-{i}", name, provider, ts(tx.now), ts(tx.now)),
            )
        pool = PoolUpsert(
            image="w:1",
            max_replicas=2,
            capabilities=["nanoclaw:harness:codex:openai"],
        )
        await fleet.upsert_pool(tx, "web", pool, "admin:test")
        # Carries the anthropic capability, but is neither wanted nor running.
        idle = pool.model_copy(
            update={"capabilities": ["nanoclaw:harness:claude:anthropic"]}
        )
        await fleet.upsert_pool(tx, "idle", idle, "admin:test")
        await fleet.upsert_pool(
            tx, "web", pool.model_copy(update={"desired_replicas": 1}), "admin:test"
        )
    async with db.read() as conn:
        drift = (await fleet.inspect(conn, "web")).capability_drift
    assert [(d.provider, d.accounts) for d in drift] == [("anthropic", ["work-claude"])]
    assert "nanoclaw:harness:claude:anthropic" in drift[0].missing
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "idle",
            PoolUpsert(
                image="w:1",
                max_replicas=2,
                desired_replicas=1,
                capabilities=["nanoclaw:harness:claude:anthropic"],
            ),
            "admin:test",
        )
    async with db.read() as conn:
        assert (await fleet.inspect(conn, "web")).capability_drift == []
