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
from argus_core.tasks import TaskCreate

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
            TaskCreate(project="global", title="local harness routing"),
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
            TaskCreate(project="global", title=title, account_providers=providers),
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
    assert t.state == "ready" and t.attempt == 0
    async with db.write() as tx:
        await accounts.release(tx, "agent-b", "done")
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
    assert t.state == "ready" and t.attempt == 0
    assert (await claim_one(db)) is not None


async def test_unreachable_local_holds_only_the_restricted_task(
    db: Database, store, agent
) -> None:
    await add_local(db, store, "strata")
    restricted = await make_task(db, ["local"], "restricted")
    plain = await make_task(db, None, "plain")
    async with db.write() as tx:
        await accounts.sync_local_health(tx, False, "down")
    got = await claim_one(db)
    assert got is not None and got.id == plain
    async with db.read() as conn:
        t = await tasks.get_task(conn, restricted)
    assert t.state == "ready" and t.attempt == 0
    async with db.write() as tx:
        await accounts.sync_local_health(tx, True)
    again = await claim_one(db, "agent-a")
    assert again is not None and again.id == restricted


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
            TaskCreate(project="global", title="x", account_providers=bad)
    ok = TaskCreate(project="global", title="x", account_providers=["local", "local"])
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
