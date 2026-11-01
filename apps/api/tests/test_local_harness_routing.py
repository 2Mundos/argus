"""Lease routing: cloud accounts beat local; local serves when cloud cannot."""

from datetime import timedelta

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import (
    ConflictError,
    LeaseConflictError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, AccountUpdate, ExhaustedReport
from argus_core.task_edit import TaskChanges
from argus_core.tasks import TaskCreate, TaskResult

A = "agent-a"
RUNTIME = ["anthropic", "openai", "local"]


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def claim_task(db: Database, agent: str = A) -> tuple[str, int]:
    async with db.write() as tx:
        await tasks.create_task(
            tx,
            TaskCreate(project="demo", title="local harness routing"),
            "admin:test",
        )
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task.id, task.attempt


async def add_cloud(
    db: Database, store, name: str, provider: str, priority: int, **kw
) -> str:
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider=provider,
                secret_ref=f"sops:{name}",
                priority=priority,
                **kw,
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def add_local(db: Database, store, name: str, priority: int = -10) -> str:
    cred = {
        "base_url": "http://host.docker.internal:8080",
        "auth_token": "argus-local-harness",
    }
    await store.put(name, cred)
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name, provider="local", credential=cred, priority=priority
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def lease(db: Database, store, agent: str = A):
    task_id, attempt = await claim_task(db, agent)
    async with db.write() as tx:
        return await accounts.lease(
            tx, agent, RUNTIME, store, task_id=task_id, attempt=attempt
        )


async def exhaust(
    db: Database, store, account_id: str, agent: str, clock, task_id: str, attempt: int
) -> None:
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            agent,
            account_id,
            ExhaustedReport(
                until=clock.now + timedelta(hours=1),
                reason="quota",
                runtime_providers=RUNTIME,
                task_id=task_id,
                attempt=attempt,
            ),
            store,
        )


async def test_cloud_wins_over_local_when_available(
    db: Database, store, agent, clock
) -> None:
    await add_local(db, store, "strata", priority=1000)
    await add_cloud(db, store, "claude", "anthropic", priority=0)
    got = await lease(db, store)
    assert got.provider == "anthropic"


async def test_local_when_anthropic_exhausted_and_openai_disabled(
    db: Database, store, agent, clock
) -> None:
    await add_local(db, store, "strata", priority=0)
    anthropic_id = await add_cloud(db, store, "claude", "anthropic", priority=100)
    openai_id = await add_cloud(db, store, "codex", "openai", priority=50)
    async with db.write() as tx:
        await accounts.update_account(
            tx, openai_id, AccountUpdate(enabled=False), "admin:test"
        )
    task_id, attempt = await claim_task(db)
    async with db.write() as tx:
        held = await accounts.lease(
            tx, A, RUNTIME, store, task_id=task_id, attempt=attempt
        )
    assert held.provider == "anthropic"
    await exhaust(db, store, anthropic_id, A, clock, task_id, attempt)
    async with db.write() as tx:
        got = await accounts.lease(
            tx, A, RUNTIME, store, task_id=task_id, attempt=attempt
        )
    assert got.provider == "local"
    assert got.credential["base_url"] == "http://host.docker.internal:8080"


# -- per-task provider restriction (local harness spec 3) --------------------------------


async def make_task(
    db: Database, providers: list[str] | None, title: str = "restricted"
) -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx,
            TaskCreate(project="demo", title=title, account_providers=providers),
            "admin:test",
        )
    return task.id


async def claim_one(db: Database, agent: str = A, providers=RUNTIME):
    async with db.write() as tx:
        return await tasks.claim(tx, agent, [], runtime_providers=providers)


async def test_restricted_lease_uses_only_local_even_when_cloud_is_usable(
    db: Database, store, agent
) -> None:
    await add_cloud(db, store, "claude", "anthropic", priority=100)
    await add_local(db, store, "strata", priority=-10)
    await make_task(db, ["local"])
    task = await claim_one(db)
    assert task is not None and task.account_providers == ["local"]
    async with db.write() as tx:
        got = await accounts.lease(
            tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )
    assert got.provider == "local"


async def test_unrestricted_task_still_gets_cloud(db: Database, store, agent) -> None:
    await add_cloud(db, store, "claude", "anthropic", priority=100)
    await add_local(db, store, "strata", priority=-10)
    assert (await lease(db, store)).provider == "anthropic"


async def test_restricted_lease_never_falls_back_to_cloud(
    db: Database, store, agent
) -> None:
    await add_cloud(db, store, "claude", "anthropic", priority=100)
    await make_task(db, ["local"])
    async with db.write() as tx:
        # no local account exists, so the claim gate skips it; force the claim
        row = await (await tx.conn.execute("SELECT id FROM tasks")).fetchone()
        await tx.conn.execute(
            "UPDATE tasks SET state = 'claimed', assignee_agent_id = ?, attempt = 1,"
            " lease_expires_at = '2999-01-01T00:00:00+00:00' WHERE id = ?",
            (A, row["id"]),
        )
        with pytest.raises(accounts.NoAccountError):
            await accounts.lease(tx, A, RUNTIME, store, task_id=row["id"], attempt=1)


async def test_restricted_task_waits_for_busy_local_without_spending_an_attempt(
    db: Database, store, agent
) -> None:
    await insert_agent(db, "agent-b")
    await add_local(db, store, "strata")  # one lease at a time
    await make_task(db, ["local"], "holder")
    holder = await claim_one(db, "agent-b")
    assert holder is not None
    async with db.write() as tx:
        await accounts.lease(
            tx, "agent-b", RUNTIME, store, task_id=holder.id, attempt=holder.attempt
        )
    second = await make_task(db, ["local"], "second")
    assert await claim_one(db) is None
    async with db.read() as conn:
        t = await tasks.get_task(conn, second)
    # nobody can serve it now: it waits for the account instead of bouncing
    assert (t.state, t.block_reason, t.attempt, t.failures) == (
        "blocked",
        "awaiting_quota",
        0,
        0,
    )
    async with db.write() as tx:
        await accounts.release(tx, "agent-b", "done")  # the freed slot wakes it
    async with db.read() as conn:
        assert (await tasks.get_task(conn, second)).state == "ready"
    freed = await claim_one(db)
    assert freed is not None and freed.id == second and freed.attempt == 1


async def test_claim_skips_restricted_task_when_runtime_lacks_provider(
    db: Database, store, agent
) -> None:
    await add_local(db, store, "strata")
    task_id = await make_task(db, ["local"])
    assert await claim_one(db, providers=["anthropic"]) is None
    assert await claim_one(db, providers=None) is None
    async with db.read() as conn:
        t = await tasks.get_task(conn, task_id)
    # no live runtime applies `local`: it waits, without an attempt (3.6)
    assert (t.state, t.block_reason, t.attempt) == ("blocked", "awaiting_quota", 0)
    # a runtime that applies local wakes it on its claim and takes it
    got = await claim_one(db)
    assert got is not None and got.id == task_id and got.attempt == 1


async def test_unreachable_local_parks_tasks_and_wakes_one_per_slot(
    db: Database, store, agent
) -> None:
    await add_local(db, store, "strata")
    restricted = await make_task(db, ["local"], "restricted")
    plain = await make_task(db, None, "plain")
    async with db.write() as tx:
        await accounts.sync_local_health(tx, False, "down")
    # the local model is the only account and it is down: neither task can run, so
    # both wait for an account without spending an attempt
    assert await claim_one(db) is None
    async with db.read() as conn:
        for task_id in (restricted, plain):
            t = await tasks.get_task(conn, task_id)
            assert (t.state, t.block_reason, t.attempt) == (
                "blocked",
                "awaiting_quota",
                0,
            )
    async with db.write() as tx:
        await accounts.sync_local_health(tx, True)
    async with db.read() as conn:
        # one lease slot is back: one task is woken, the other keeps waiting
        states = {i: (await tasks.get_task(conn, i)).state for i in (restricted, plain)}
    woken = [i for i, state in states.items() if state == "ready"]
    assert len(woken) == 1
    again = await claim_one(db, "agent-a")
    assert again is not None and again.id == woken[0] and again.attempt == 1


async def test_restricted_lease_still_requires_the_live_claim(
    db: Database, store, agent
) -> None:
    await add_local(db, store, "strata")
    await make_task(db, ["local"])
    task = await claim_one(db)
    assert task is not None
    await insert_agent(db, "agent-b")
    async with db.write() as tx:
        with pytest.raises(LeaseConflictError):
            await accounts.lease(
                tx, "agent-b", RUNTIME, store, task_id=task.id, attempt=task.attempt
            )
    async with db.write() as tx:
        with pytest.raises(LeaseConflictError):
            await accounts.lease(
                tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt + 1
            )
    async with db.write() as tx:
        with pytest.raises((LeaseConflictError, NotFoundError)):
            await accounts.lease(
                tx, A, RUNTIME, store, task_id="no-such-task", attempt=1
            )


def test_account_providers_are_validated() -> None:
    for bad in (["gitea"], ["cursor"], ["bogus"], []):
        with pytest.raises(ValueError):
            TaskCreate(project="demo", title="x", account_providers=bad)
    ok = TaskCreate(project="demo", title="x", account_providers=["local", "local"])
    assert ok.account_providers == ["local"]


async def test_account_providers_edit_rules(db: Database, store, agent) -> None:
    task_id = await make_task(db, None)
    async with db.read() as conn:
        version = (await tasks.get_task(conn, task_id)).version
    async with db.write() as tx:
        edited = await tasks.edit_task(
            tx, task_id, TaskChanges(account_providers=["local"]), version, "admin:t"
        )
    assert edited.account_providers == ["local"]
    with pytest.raises(ValidationFailedError):
        async with db.write() as tx:
            await tasks.edit_task(
                tx,
                task_id,
                TaskChanges(account_providers=["gitea"]),
                edited.version,
                "admin:t",
            )
    async with db.write() as tx:
        cleared = await tasks.edit_task(
            tx, task_id, TaskChanges(account_providers=None), edited.version, "admin:t"
        )
    assert cleared.account_providers is None
    claimed = await claim_one(db)
    assert claimed is not None
    with pytest.raises(ConflictError):
        async with db.write() as tx:
            await tasks.edit_task(
                tx,
                task_id,
                TaskChanges(account_providers=["local"]),
                claimed.version,
                "admin:t",
            )


# -- waiting for an account: no retry storm, no thundering herd (account selection 3.6)


async def _busy_local(db: Database, store) -> str:
    """The only account is local (one lease) and agent-b is working on it."""
    await insert_agent(db, "agent-b")
    await add_local(db, store, "strata")
    await make_task(db, ["local"], "holder")
    holder = await claim_one(db, "agent-b")
    assert holder is not None
    async with db.write() as tx:
        await accounts.lease(
            tx, "agent-b", RUNTIME, store, task_id=holder.id, attempt=holder.attempt
        )
    return holder.id


async def test_agent_without_an_account_does_not_claim_unrestricted_work(
    db: Database, store, agent
) -> None:
    await _busy_local(db, store)
    plain = await make_task(db, None, "plain")
    for _ in range(3):  # a polling agent never bounces it
        assert await claim_one(db) is None
    async with db.read() as conn:
        t = await tasks.get_task(conn, plain)
    assert (t.state, t.block_reason, t.attempt, t.failures) == (
        "blocked",
        "awaiting_quota",
        0,
        0,
    )


async def test_quota_fail_with_no_account_left_waits_instead_of_requeueing(
    db: Database, store, agent
) -> None:
    await _busy_local(db, store)
    plain = await make_task(db, None, "plain")
    async with db.write() as tx:
        # claimed before the account filled up (e.g. a race between two agents)
        await tx.conn.execute(
            "UPDATE tasks SET state = 'claimed', assignee_agent_id = ?, attempt = 1,"
            " lease_expires_at = '2999-01-01T00:00:00+00:00' WHERE id = ?",
            (A, plain),
        )
        await tasks.fail(
            tx, A, plain, 1, "no usable provider account for claimed task", "quota"
        )
    async with db.read() as conn:
        t = await tasks.get_task(conn, plain)
    assert (t.state, t.block_reason, t.attempt, t.failures) == (
        "blocked",
        "awaiting_quota",
        1,
        0,
    )
    assert t.error == "no usable provider account for claimed task"


async def test_quota_fail_requeues_when_an_account_is_free(
    db: Database, store, agent
) -> None:
    await add_local(db, store, "strata")
    plain = await make_task(db, None, "plain")
    got = await claim_one(db)
    assert got is not None and got.id == plain
    async with db.write() as tx:
        await tasks.fail(tx, A, plain, got.attempt, "transient", "quota")
    async with db.read() as conn:
        t = await tasks.get_task(conn, plain)
    assert (t.state, t.failures) == ("ready", 0)


async def test_released_lease_wakes_only_as_many_tasks_as_it_frees(
    db: Database, store, agent
) -> None:
    holder = await _busy_local(db, store)
    waiting = [await make_task(db, None, f"plain-{i}") for i in range(3)]
    assert await claim_one(db) is None  # parks all three
    async with db.write() as tx:
        await tasks.complete(tx, "agent-b", holder, 1, TaskResult(summary="done"))
        await accounts.release(tx, "agent-b", "released")
    async with db.read() as conn:
        states = [(await tasks.get_task(conn, i)).state for i in waiting]
    assert states.count("ready") == 1 and states.count("blocked") == 2
    woken = waiting[states.index("ready")]
    got = await claim_one(db)
    assert got is not None and got.id == woken and got.failures == 0


async def test_an_idle_holder_keeps_the_task_ready_for_itself(
    db: Database, store, agent
) -> None:
    await insert_agent(db, "agent-b")
    await add_local(db, store, "strata")
    first = await make_task(db, None, "first")
    got = await claim_one(db, "agent-b")
    assert got is not None and got.id == first
    async with db.write() as tx:
        await accounts.lease(
            tx, "agent-b", RUNTIME, store, task_id=got.id, attempt=got.attempt
        )
        await tasks.complete(
            tx, "agent-b", first, got.attempt, TaskResult(summary="ok")
        )
    second = await make_task(db, None, "second")
    # agent-a holds nothing and the only slot is agent-b's idle lease: a skips it
    assert await claim_one(db) is None
    async with db.read() as conn:
        assert (await tasks.get_task(conn, second)).state == "ready"
    again = await claim_one(db, "agent-b")
    assert again is not None and again.id == second


CUSTOM = {
    "base_url": "https://openrouter.ai/api/v1",
    "label": "openrouter",
    "engine": {"model": "deepseek/deepseek-chat"},
}


async def _add_custom(db: Database, store, name: str = "openrouter") -> str:
    await store.put(name, {"api_key": "sk-or-v1-FAKEKEYFAKEKEY"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider="custom",
                secret_ref=f"sops:{name}",
                config=CUSTOM,  # type: ignore[arg-type]
                max_leases=1,
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def test_an_account_no_live_worker_can_run_is_no_capacity(
    db: Database, store, agent
) -> None:
    """A free `custom` account counts for nothing while every live runtime applies
    only anthropic/openai/local: the task parks instead of sitting ready."""
    await _busy_local(db, store)
    await _add_custom(db, store)
    plain = await make_task(db, None, "plain")
    assert await claim_one(db) is None
    async with db.read() as conn:
        t = await tasks.get_task(conn, plain)
        assert (t.state, t.block_reason, t.attempt) == ("blocked", "awaiting_quota", 0)
    async with db.write() as tx:
        assert await tasks.engine_capacity(tx) == []
        assert await tasks.wake_waiting(tx) == []


async def test_a_worker_that_runs_the_provider_wakes_the_waiting_task(
    db: Database, store, agent
) -> None:
    await _busy_local(db, store)
    await _add_custom(db, store)
    plain = await make_task(db, None, "plain")
    assert await claim_one(db) is None  # parked: nobody runs custom
    await insert_agent(db, "agent-c")
    got = await claim_one(db, "agent-c", [*RUNTIME, "custom"])
    assert got is not None and got.id == plain and got.failures == 0


async def test_lost_workers_do_not_count_for_their_providers(
    db: Database, store, agent
) -> None:
    await _busy_local(db, store)
    await _add_custom(db, store)
    await insert_agent(db, "agent-c")
    other = await make_task(db, ["custom"], "custom-only")
    got = await claim_one(db, "agent-c", ["custom"])
    assert got is not None and got.id == other
    async with db.write() as tx:
        await tasks.complete(
            tx, "agent-c", other, got.attempt, TaskResult(summary="ok")
        )
        await tx.conn.execute(
            "UPDATE agents SET state = 'lost', ended_at = ? WHERE id = 'agent-c'",
            ("2026-10-21T00:00:00+00:00",),
        )
    plain = await make_task(db, None, "plain")
    assert await claim_one(db) is None
    async with db.read() as conn:
        assert (await tasks.get_task(conn, plain)).block_reason == "awaiting_quota"
