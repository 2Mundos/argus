"""Per-task harness account pin (docs/specs/task-models.spec.md 3.4)."""

import json

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.accounts.service import NoAccountError
from argus_api.db import Database
from argus_api.errors import ValidationFailedError
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, AccountUpdate
from argus_core.task_edit import TaskChanges
from argus_core.tasks import ExecutorModel, HarnessModel, TaskCreate

A = "agent-a"
RUNTIME = ["anthropic", "openai", "custom", "local", "cursor"]
CUSTOM = {
    "base_url": "https://openrouter.ai/api/v1",
    "label": "openrouter",
    "engine": {"model": "deepseek/deepseek-v4.1-flash"},
}
LOCAL = {
    "base_url": "http://host.docker.internal:8080",
    "auth_token": "argus-local-harness",
}
COMPOSER = ExecutorModel(executor="agent", model="composer-2.5")


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add(db: Database, store, account_id: str, name: str, provider: str, **kw):
    if provider == "local":
        await store.put(account_id, LOCAL)
        body = AccountCreate(name=name, provider=provider, credential=LOCAL, **kw)
    else:
        await store.put(account_id, {"api_key": f"key-{account_id}"})
        body = AccountCreate(
            name=name,
            provider=provider,
            secret_ref=f"sops:{account_id}",
            config=CUSTOM if provider == "custom" else None,  # type: ignore[arg-type]
            **kw,
        )
    async with db.write() as tx:
        await accounts.create_account(
            tx, account_id, body, f"sops:{account_id}", "admin:test", store
        )


async def fleet(db: Database, store) -> None:
    """Two cloud accounts that outrank Strata and OpenRouter, as in production."""
    await add(db, store, "acc-claude", "claude-max", "anthropic", priority=120)
    await add(db, store, "acc-codex", "codex-pro", "openai", priority=100)
    await add(db, store, "acc-or", "openrouter-deepseek-v4.1-flash", "custom")
    await add(db, store, "acc-or2", "openrouter-other", "custom", priority=50)
    await add(db, store, "acc-strata", "strata-local", "local", priority=-10)


async def make_task(db: Database, **kw) -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="global", title="pinned", **kw), "admin:test"
        )
    return task.id


async def claim(db: Database, runtime: list[str] = RUNTIME):
    async with db.write() as tx:
        return await tasks.claim(tx, A, [], runtime_providers=runtime)


async def engine_lease(db: Database, store, task):
    async with db.write() as tx:
        return await accounts.lease(
            tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


@pytest.mark.parametrize(
    ("ref", "account_id", "provider"),
    [
        ("openrouter-deepseek-v4.1-flash", "acc-or", "custom"),
        ("acc-strata", "acc-strata", "local"),
    ],
)
async def test_pin_forces_the_engine_lease_onto_that_account(
    db: Database, store, agent, ref, account_id, provider
) -> None:
    await fleet(db, store)
    task_id = await make_task(db, harness_account=ref, executor_model=COMPOSER)
    assert (await get(db, task_id)).harness_account == account_id  # name -> id
    task = await claim(db)
    assert task is not None and task.id == task_id
    lease = await engine_lease(db, store, task)
    assert (lease.account_id, lease.provider) == (account_id, provider)
    async with db.read() as conn:
        attempts = await tasks.attempt_definitions(conn, task_id)
    assert attempts[0].definition["harness_account"] == account_id


async def test_pin_waits_as_awaiting_quota_while_its_account_cannot_serve(
    db: Database, store, agent
) -> None:
    await fleet(db, store)
    async with db.write() as tx:
        await accounts.update_account(
            tx, "acc-or", AccountUpdate(enabled=False), "admin:test"
        )
    task_id = await make_task(db, harness_account="acc-or")
    # The other custom account and both cloud accounts are usable: it never runs there.
    assert await claim(db) is None
    task = await get(db, task_id)
    assert task.state == "blocked" and task.block_reason == "awaiting_quota"
    assert task.attempt == 0 and task.failures == 0
    async with db.write() as tx:
        await accounts.update_account(
            tx, "acc-or", AccountUpdate(enabled=True), "admin:test"
        )
        await tasks.wake_waiting(tx)
    task = await claim(db)
    assert task is not None and task.id == task_id
    assert (await engine_lease(db, store, task)).account_id == "acc-or"


async def test_wake_spends_only_the_pinned_accounts_slot(
    db: Database, store, agent
) -> None:
    await fleet(db, store)
    async with db.write() as tx:
        await accounts.update_account(
            tx, "acc-strata", AccountUpdate(enabled=False), "admin:test"
        )
    waiting = await make_task(db, harness_account="strata-local")
    assert await claim(db) is None
    async with db.write() as tx:
        assert await tasks.wake_waiting(tx) == []  # cloud capacity is not Strata's
    async with db.write() as tx:  # enabling it wakes exactly the task pinned to it
        await accounts.update_account(
            tx, "acc-strata", AccountUpdate(enabled=True), "admin:test"
        )
    assert (await get(db, waiting)).state == "ready"


async def test_the_pinned_account_must_be_held_or_usable_at_claim(
    db: Database, store, agent
) -> None:
    await add(db, store, "acc-or", "openrouter-a", "custom", max_leases=1)
    await add(db, store, "acc-or2", "openrouter-b", "custom", priority=10)
    await insert_agent(db, "agent-b")
    first = await make_task(db, harness_account="acc-or", priority=5)
    async with db.write() as tx:
        held = await tasks.claim(tx, "agent-b", [], runtime_providers=RUNTIME)
    assert held is not None and held.id == first
    async with db.write() as tx:
        await accounts.lease(
            tx, "agent-b", RUNTIME, store, task_id=held.id, attempt=held.attempt
        )
    second = await make_task(db, harness_account="acc-or")
    assert await claim(db) is None  # acc-or is full; acc-or2 is free but not pinned
    assert (await get(db, second)).block_reason == "awaiting_quota"


async def test_lease_refuses_any_other_account(db: Database, store, agent) -> None:
    await fleet(db, store)
    task_id = await make_task(db, harness_account="acc-or")
    task = await claim(db)
    assert task is not None and task.id == task_id
    async with db.write() as tx:
        await accounts.update_account(
            tx, "acc-or", AccountUpdate(enabled=False), "admin:test"
        )
    with pytest.raises(NoAccountError):
        await engine_lease(db, store, task)


async def test_executor_lease_is_not_pinned(db: Database, store, agent) -> None:
    await fleet(db, store)
    task = None
    await make_task(db, harness_account="acc-or")
    task = await claim(db)
    assert task is not None
    async with db.write() as tx:
        executor = await accounts.lease(
            tx, A, RUNTIME, store, "executor", task_id=task.id, attempt=task.attempt
        )
    assert executor.account_id == "acc-claude"


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"harness_account": "nope"}, "no account 'nope'"),
        (
            {"harness_account": "acc-or", "account_providers": ["anthropic"]},
            "excluded by account_providers",
        ),
        (
            {
                "harness_account": "acc-or",
                "harness_model": HarnessModel(engine="claude", model="claude-opus-5-5"),
            },
            "needs anthropic",
        ),
        (
            {
                "harness_account": "strata-local",
                "executor_model": ExecutorModel(
                    executor="claude", model="claude-sonnet-5-5"
                ),
            },
            "delegates only to the Cursor agent",
        ),
    ],
)
async def test_pin_is_validated_at_creation(db: Database, store, kw, why) -> None:
    await fleet(db, store)
    with pytest.raises(ValidationFailedError, match=why):
        await make_task(db, **kw)


async def test_pin_edit_rules(db: Database, store, agent) -> None:
    await fleet(db, store)
    task_id = await make_task(db, executor_model=COMPOSER)
    version = (await get(db, task_id)).version
    async with db.write() as tx:
        edited = await tasks.edit_task(
            tx, task_id, TaskChanges(harness_account="strata-local"), version, "a:t"
        )
    assert edited.harness_account == "acc-strata"
    # a later edit of another field is checked against the stored pin
    with pytest.raises(ValidationFailedError, match="Cursor agent"):
        async with db.write() as tx:
            await tasks.edit_task(
                tx,
                task_id,
                TaskChanges(
                    executor_model=ExecutorModel(executor="codex", model="gpt-6.1-sol")
                ),
                edited.version,
                "a:t",
            )
    async with (
        db.read() as conn,
        conn.execute("SELECT data FROM events WHERE kind = 'task.edited'") as cur,
    ):
        changes = json.loads((await cur.fetchone())["data"])["changes"]
    assert changes["harness_account"] == {"from": None, "to": "acc-strata"}
    async with db.write() as tx:
        cleared = await tasks.edit_task(
            tx, task_id, TaskChanges(harness_account=None), edited.version, "a:t"
        )
    assert cleared.harness_account is None
    task = await claim(db)
    assert task is not None
    assert (await engine_lease(db, store, task)).account_id == "acc-claude"
