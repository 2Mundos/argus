"""Lease routing: cloud accounts beat local; local serves when cloud cannot."""

from datetime import timedelta

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, AccountUpdate, ExhaustedReport
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
