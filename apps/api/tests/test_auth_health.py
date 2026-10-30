"""Auth-health timestamps and account.auth_rejected events (account-usage.spec.md 3.5)."""

import json
from datetime import timedelta

import pytest
from api_support import FakeClock, insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import (
    AccountCreate,
    AuthRejectedReport,
    ExhaustedReport,
    UsageReport,
)
from argus_core.tasks import TaskCreate

A = "agent-a"
RUNTIME = ["anthropic", "openai", "custom", "local"]


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add(db: Database, store, name: str, provider: str) -> str:
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        acct = await accounts.create_account(
            tx,
            name,
            AccountCreate(name=name, provider=provider, secret_ref=f"sops:{name}"),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return acct.id


async def events(db: Database, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT kind, data FROM events WHERE kind = ? ORDER BY id",
            (kind,),
        ) as cur,
    ):
        rows = await cur.fetchall()
    return [{"kind": r[0], "data": json.loads(r[1])} for r in rows]


async def claim_and_lease(db: Database, store):
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="global", title="t"), "u")
    async with db.write() as tx:
        task = await tasks.claim(tx, A, [])
    assert task is not None
    async with db.write() as tx:
        lease = await accounts.lease(
            tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )
    assert lease is not None
    return task, lease


async def test_last_accepted_from_measured_output(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    account_id = await add(db, store, "codex-1", "openai")
    task, _lease = await claim_and_lease(db, store)
    at = clock.now - timedelta(hours=1)
    clock.now = at
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            account_id,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                output_tokens=42,
                input_tokens=1,
                role="engine",
            ),
        )
    async with db.read() as conn:
        acct = await accounts.get_account(conn, account_id, clock.now)
    assert acct.last_accepted_at == at
    assert acct.last_rejected_at is None


async def test_last_accepted_ignores_not_reported_and_zero_output(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    account_id = await add(db, store, "claude-1", "anthropic")
    task, _lease = await claim_and_lease(db, store)
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            account_id,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                measurement="not_reported",
                reason="unknown",
            ),
        )
        await accounts.record_usage(
            tx,
            account_id,
            A,
            UsageReport(
                task_id=task.id,
                attempt=task.attempt,
                output_tokens=0,
                input_tokens=5,
            ),
        )
    async with db.read() as conn:
        acct = await accounts.get_account(conn, account_id, clock.now)
    assert acct.last_accepted_at is None


async def test_auth_rejected_endpoint_and_last_rejected(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    account_id = await add(db, store, "claude-1", "anthropic")
    task, _lease = await claim_and_lease(db, store)
    async with db.write() as tx:
        await accounts.auth_rejected(
            tx,
            A,
            account_id,
            AuthRejectedReport(
                task_id=task.id,
                attempt=task.attempt,
                role="engine",
                source="structured",
            ),
        )
    async with db.read() as conn:
        acct = await accounts.get_account(conn, account_id, clock.now)
    assert acct.last_rejected_at is not None
    [event] = await events(db, "account.auth_rejected")
    assert event["data"]["reason"] == "Claude authentication failed"
    assert "api_key" not in str(event["data"])


async def test_exhausted_auth_expired_also_writes_auth_rejected(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    account_id = await add(db, store, "codex-1", "openai")
    task, _lease = await claim_and_lease(db, store)
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            A,
            account_id,
            ExhaustedReport(
                reason="OpenAI/Codex authentication expired",
                reason_code="auth_expired",
                source="structured",
                reset_status="absent",
                role="engine",
                runtime_providers=RUNTIME,
                task_id=task.id,
                attempt=task.attempt,
            ),
            store,
        )
    async with db.read() as conn:
        acct = await accounts.get_account(conn, account_id, clock.now)
    assert acct.last_rejected_at is not None
    assert await events(db, "account.exhausted")
    assert len(await events(db, "account.auth_rejected")) == 1
