"""A harness pin is a preference: it falls back to the other allowed providers
(docs/specs/task-models.spec.md 3.2-3.4)."""

import json
from datetime import timedelta

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.accounts.service import NoAccountError
from argus_api.db import Database, ts
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, AccountUpdate, ExhaustedReport
from argus_core.tasks import ExecutorModel, HarnessModel, TaskCreate

A = "agent-a"
RUNTIME = ["anthropic", "openai", "cursor"]
OPUS = HarnessModel(engine="claude", model="claude-opus-5-5")


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add(db: Database, store, name: str, provider: str, **kw) -> None:
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name, provider=provider, secret_ref=f"sops:{name}", **kw
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )


async def exhaust(db: Database, name: str, minutes: int = 60) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE accounts SET state = 'exhausted', exhausted_until = ? WHERE id = ?",
            (ts(tx.now + timedelta(minutes=minutes)), name),
        )


async def make(db: Database, **kw) -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="demo", title="pinned", **kw), "admin:test"
        )
    return task.id


async def claim(db: Database):
    async with db.write() as tx:
        return await tasks.claim(tx, A, [], runtime_providers=RUNTIME)


async def lease(db: Database, store, task):
    async with db.write() as tx:
        return await accounts.lease(
            tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )


async def park(db: Database) -> list[str]:
    """The channel's `POST /accounts/awaiting-quota` after a lease found nothing."""
    async with db.write() as tx:
        return await accounts.mark_awaiting_quota(tx, A, [], RUNTIME)


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def events(db: Database, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute("SELECT data FROM events WHERE kind = ?", (kind,)) as cur,
    ):
        return [json.loads(r[0]) for r in await cur.fetchall()]


@pytest.mark.parametrize(
    ("setup", "reason"),
    [("exhausted", "exhausted"), ("disabled", "disabled")],
)
async def test_exhausted_or_disabled_pin_falls_back_without_a_block(
    db: Database, store, agent, setup, reason
) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    if setup == "exhausted":
        await exhaust(db, "claude")
    else:
        async with db.write() as tx:
            await accounts.update_account(
                tx, "claude", AccountUpdate(enabled=False), "admin:test"
            )
    task_id = await make(db, harness_model=OPUS)
    task = await claim(db)
    assert task is not None and task.id == task_id and task.state == "claimed"
    assert (await lease(db, store, task)).provider == "openai"
    [event] = await events(db, "task.model_switched")
    assert event["reason"] == reason and event["attempt"] == task.attempt
    assert event["from"] == {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "account_id": None,
    }
    assert event["to"]["provider"] == "openai"
    assert event["to"]["account_id"] == "codex"
    assert "key-codex" not in str(event)
    async with db.read() as conn:
        definition = (await tasks.attempt_definitions(conn, task_id))[0].definition
        assert await tasks.attempt_model_switch(conn, task_id, 1) == event
    assert definition["model_switch"] == event
    assert definition["harness_model"]["model"] == "claude-opus-5-5"  # pin kept


async def test_offline_pin_falls_back(db: Database, store, agent) -> None:
    await add(db, store, "codex", "openai", priority=0)
    task_id = await make(db, harness_model=OPUS)  # no Anthropic account at all
    task = await claim(db)
    assert task is not None and task.id == task_id
    await lease(db, store, task)
    assert (await events(db, "task.model_switched"))[0]["reason"] == "offline"


async def test_fallback_after_a_quota_exhaustion(db: Database, store, agent) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    task_id = await make(db, harness_model=OPUS)
    task = await claim(db)
    assert task is not None
    held = await lease(db, store, task)
    assert held.provider == "anthropic"
    async with db.write() as tx:
        result = await accounts.exhausted(
            tx,
            A,
            "claude",
            ExhaustedReport(
                reason="Rate limit",
                runtime_providers=RUNTIME,
                task_id=task.id,
                attempt=task.attempt,
            ),
            store,
        )
    assert not result.task_blocked and result.next_provider == "openai"
    assert (await get(db, task_id)).block_reason is None


async def test_every_allowed_provider_spent_blocks_awaiting_quota(
    db: Database, store, agent
) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    await exhaust(db, "claude")
    await exhaust(db, "codex")
    task_id = await make(db, harness_model=OPUS)
    assert await claim(db) is None
    assert await park(db) == [task_id]
    task = await get(db, task_id)
    assert task.state == "blocked" and task.block_reason == "awaiting_quota"
    assert task.attempt == 0 and task.failures == 0


async def test_restricted_task_blocks_when_only_other_providers_are_usable(
    db: Database, store, agent
) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    await exhaust(db, "claude")
    task_id = await make(db, harness_model=OPUS, account_providers=["anthropic"])
    assert await claim(db) is None
    assert (await get(db, task_id)).block_reason == "awaiting_quota"


async def test_waiting_task_wakes_when_a_provider_frees_up(
    db: Database, store, agent
) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    await exhaust(db, "claude")
    await exhaust(db, "codex")
    task_id = await make(db, harness_model=OPUS)
    assert await claim(db) is None
    assert await park(db) == [task_id]
    # the other provider is refilled (its reset passed) and a wake runs
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE accounts SET exhausted_until = ? WHERE id = 'codex'",
            (ts(tx.now - timedelta(minutes=1)),),
        )
        assert await tasks.wake_waiting(tx) == [task_id]
    task = await claim(db)
    assert task is not None and task.id == task_id
    assert (await lease(db, store, task)).provider == "openai"


async def test_waiting_task_wakes_when_an_account_is_enabled(
    db: Database, store, agent
) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    async with db.write() as tx:
        await accounts.update_account(
            tx, "codex", AccountUpdate(enabled=False), "admin:test"
        )
    await exhaust(db, "claude")
    task_id = await make(db, harness_model=OPUS)
    assert await claim(db) is None
    async with db.write() as tx:
        await accounts.update_account(
            tx, "codex", AccountUpdate(enabled=True), "admin:test"
        )
    assert (await get(db, task_id)).state == "ready"
    task = await claim(db)
    assert task is not None and task.id == task_id


async def test_pin_is_kept_while_any_pinned_account_is_usable(
    db: Database, store, agent
) -> None:
    await add(db, store, "claude-a", "anthropic", priority=100)
    await add(db, store, "claude-b", "anthropic", priority=10)
    await add(db, store, "codex", "openai", priority=500)
    await exhaust(db, "claude-a")
    await make(db, harness_model=OPUS)
    task = await claim(db)
    assert task is not None
    got = await lease(db, store, task)
    assert (got.provider, got.account_id) == ("anthropic", "claude-b")
    assert await events(db, "task.model_switched") == []
    async with db.read() as conn:
        definition = (await tasks.attempt_definitions(conn, task.id))[0].definition
    assert definition.get("model_switch") is None


async def test_a_held_fallback_account_is_recorded_for_the_claim(
    db: Database, store, agent
) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    first = await make(db, account_providers=["openai"])
    task = await claim(db)
    assert task is not None and task.id == first
    assert (await lease(db, store, task)).account_id == "codex"  # the agent holds it
    async with db.write() as tx:
        await tasks.cancel(tx, first, "admin:test")
    await exhaust(db, "claude")
    pinned = await make(db, harness_model=OPUS)
    async with db.write() as tx:
        claimed, reserved = await accounts.claim(tx, A, [], RUNTIME, store)
    assert claimed is not None and claimed.id == pinned and reserved is None
    async with db.read() as conn:
        switch = await tasks.attempt_model_switch(conn, pinned, claimed.attempt)
    assert switch is not None and switch["to"]["account_id"] == "codex"
    assert switch["reason"] == "exhausted"


async def test_no_account_error_when_nothing_allowed_can_lease(
    db: Database, store, agent
) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    task_id = await make(db, harness_model=OPUS)
    task = await claim(db)
    assert task is not None and task.id == task_id
    await exhaust(db, "claude")
    with pytest.raises(NoAccountError):
        await lease(db, store, task)


async def test_executor_pin_stays_strict(db: Database, store, agent) -> None:
    await add(db, store, "claude", "anthropic", priority=100)
    await add(db, store, "codex", "openai", priority=0)
    await exhaust(db, "codex")
    await make(db, executor_model=ExecutorModel(executor="codex", model="gpt-6.1-sol"))
    task = await claim(db)
    assert task is not None
    async with db.write() as tx:
        with pytest.raises(NoAccountError):
            await accounts.lease(
                tx, A, RUNTIME, store, "executor", task_id=task.id, attempt=1
            )
