"""Auth-health timestamps and account.auth_rejected events (account-usage.spec.md 3.5)."""

import json
from datetime import timedelta

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.events import record as record_event
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
        await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "u")
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


async def report(db: Database, account_id: str, task, **usage) -> None:
    async with db.write() as tx:
        await accounts.record_usage(
            tx,
            account_id,
            A,
            UsageReport(task_id=task.id, attempt=task.attempt, role="engine", **usage),
        )


async def complete(db: Database, task) -> None:
    async with db.write() as tx:
        await tasks.complete(tx, A, task.id, task.attempt, DONE)


async def reject(db: Database, account_id: str, task) -> None:
    async with db.write() as tx:
        await accounts.auth_rejected(
            tx,
            A,
            account_id,
            AuthRejectedReport(task_id=task.id, attempt=task.attempt),
        )


async def health(db: Database, clock: FakeClock) -> dict[str, tuple]:
    async with db.read() as conn:
        listed = {
            a.id: (a.last_accepted_at, a.last_rejected_at)
            for a in await accounts.list_accounts(conn, clock.now)
        }
        for account_id, pair in listed.items():
            one = await accounts.get_account(conn, account_id, clock.now)
            assert (one.last_accepted_at, one.last_rejected_at) == pair
    return listed


async def test_auth_health_is_per_account(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    """Another account's accepted output and rejections never show on this one, and
    none of this account's own non-evidence rows count: a cost-only report (NULL
    output_tokens), zero output, the attempt's `not_reported` placeholder, an
    account-less placeholder, and events of another kind or subject type."""
    other = await add(db, store, "claude-other", "anthropic")
    first, lease = await claim_and_lease(db, store)
    assert lease.account_id == other
    accepted_at = clock.now
    await report(db, other, first, output_tokens=7, input_tokens=1)
    clock.now += timedelta(seconds=1)
    rejected_at = clock.now
    await reject(db, other, first)
    await complete(db, first)

    # Higher priority, so the next leases take it.
    await store.put("claude-target", {"api_key": "key-claude-target"})
    async with db.write() as tx:
        target = (
            await accounts.create_account(
                tx,
                "claude-target",
                AccountCreate(
                    name="claude-target",
                    provider="anthropic",
                    secret_ref="sops:claude-target",
                    priority=10,
                ),
                "sops:claude-target",
                "admin:test",
                store,
            )
        ).id
    clock.now += timedelta(seconds=1)
    second, lease = await claim_and_lease(db, store)
    assert lease.account_id == target
    await report(db, target, second, cost_usd=0.25, input_tokens=3, run_key="cost")
    await report(db, target, second, output_tokens=0, input_tokens=3, run_key="zero")
    await complete(db, second)
    third, lease = await claim_and_lease(db, store)
    assert lease.account_id == target
    await complete(db, third)  # no report: the attempt's placeholder
    async with db.write() as tx:
        await accounts.release(tx, A, "done", "engine")
        await tasks.create_task(tx, TaskCreate(project="demo", title="bare"), "u")
    clock.now += timedelta(seconds=1)
    async with db.write() as tx:
        bare = await tasks.claim(tx, A, [])
    assert bare is not None
    await complete(db, bare)  # no lease at all: an account-less placeholder
    async with db.write() as tx:
        await record_event(tx, "agent:x", "account", target, "account.auth_ok", {})
        await record_event(tx, "agent:x", "agent", target, "account.auth_rejected", {})
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT account_id, measurement, output_tokens, substr(run_key, 1, 3)"
            " FROM account_usage WHERE task_id IN (?, ?, ?) ORDER BY id",
            (second.id, third.id, bare.id),
        ) as cur,
    ):
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [
        (target, "measured", None, "cos"),
        (target, "measured", 0, "zer"),
        (target, "not_reported", None, "a1:"),
        (None, "not_reported", None, "a1:"),
    ]

    assert await health(db, clock) == {
        other: (accepted_at, rejected_at),
        target: (None, None),
    }

    # Evidence on the target moves only the target.
    fourth, lease = await claim_and_lease(db, store)
    assert lease.account_id == target
    clock.now += timedelta(seconds=1)
    target_accepted = clock.now
    await report(db, target, fourth, output_tokens=1)
    clock.now += timedelta(seconds=1)
    await reject(db, target, fourth)
    assert await health(db, clock) == {
        other: (accepted_at, rejected_at),
        target: (target_accepted, clock.now),
    }


async def test_ordinary_fail_after_auth_rejected_does_not_exhaust(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    """The channel's follow-up `provider_failure` (the fixed reason) is a plain
    attempt failure: Claude stays available and no failover is recorded."""
    account_id = await add(db, store, "claude-1", "anthropic")
    task, _lease = await claim_and_lease(db, store)
    await reject(db, account_id, task)
    async with db.write() as tx:
        failed = await accounts.fail_task(
            tx,
            A,
            task.id,
            task.attempt,
            "Claude authentication failed",
            "provider_failure",
            store,
        )
    assert failed.failures == 1
    async with db.read() as conn:
        acct = await accounts.get_account(conn, account_id, clock.now)
    assert (acct.state, acct.exhausted_until) == ("available", None)
    assert await events(db, "account.exhausted") == []
    assert await events(db, "account.failover") == []


async def test_quota_exhaustion_sets_neither_timestamp(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    account_id = await add(db, store, "codex-quota", "openai")
    task, _lease = await claim_and_lease(db, store)
    async with db.write() as tx:
        await accounts.exhausted(
            tx,
            A,
            account_id,
            ExhaustedReport(
                reason="rate limit reached",
                reason_code="quota_exhausted",
                source="structured",
                reset_status="absent",
                role="engine",
                runtime_providers=RUNTIME,
                task_id=task.id,
                attempt=task.attempt,
            ),
            store,
        )
    assert await events(db, "account.exhausted")
    assert await health(db, clock) == {account_id: (None, None)}


async def test_credential_state_is_derived_from_the_store(
    db: Database, store: MemorySecretStore, clock: FakeClock
):
    ok = await add(db, store, "claude-ok", "anthropic")
    gone = await add(db, store, "claude-gone", "anthropic")
    await store.delete("claude-gone")
    expired = clock.now - timedelta(hours=1)
    valid = clock.now + timedelta(hours=1)

    async def oauth(name: str, at) -> str:
        await store.put(
            name,
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "refreshToken": "r",
                    "expiresAt": int(at.timestamp() * 1000),
                }
            },
        )
        async with db.write() as tx:
            acct = await accounts.create_account(
                tx,
                name,
                AccountCreate(
                    name=name, provider="anthropic", secret_ref=f"sops:{name}"
                ),
                f"sops:{name}",
                "admin:test",
                store,
            )
        return acct.id

    old = await oauth("claude-old", expired)
    live = await oauth("claude-live", valid)
    async with db.read() as conn:
        listed = await accounts.list_accounts(conn, clock.now)
    got = {
        a.id: a.credential for a in accounts.with_credential(listed, store, clock.now)
    }
    assert got[ok].state == "present" and got[ok].expires_at is None
    assert got[gone].state == "missing"
    assert got[old].state == "expired" and got[old].expires_at == expired
    assert got[live].state == "present" and got[live].expires_at == valid
    assert all(a.credential is None for a in listed)
