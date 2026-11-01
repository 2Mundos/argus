"""A custom account serves a role only when that role has a model: the catalog
(`engine_pool`, `earliest_reset`), the ranking and the lease agree, and only the
role without a model is lost. Native providers carry no config and are untouched."""

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.accounts.eligibility import engine_pool
from argus_api.accounts.service import NoAccountError
from argus_api.db import Database
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate
from argus_core.tasks import TaskCreate

A, B = "agent-a", "agent-b"
BASE = "https://openrouter.ai/api/v1"
RUNTIME = ["custom", "anthropic", "openai", "cursor"]


def cfg(engine: str | None, executor: str | None) -> dict:
    return {
        "base_url": BASE,
        "label": "openrouter",
        "engine": {"model": engine},
        "executor": {"model": executor},
    }


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)


async def add(db: Database, store: MemorySecretStore, name: str, provider: str, **kw):
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name, provider=provider, secret_ref=f"sops:{name}", **kw
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def custom(db, store, name, engine, executor, **kw) -> str:
    return await add(db, store, name, "custom", config=cfg(engine, executor), **kw)


async def claim(db: Database, agent: str = A) -> tuple[str, int]:
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "admin:test")
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task.id, task.attempt


async def lease(db, store, role, agent=A, runtime=RUNTIME, **kw):
    task_id, attempt = await claim(db, agent)
    async with db.write() as tx:
        return await accounts.lease(
            tx, agent, runtime, store, role, task_id=task_id, attempt=attempt, **kw
        )


async def pool(db: Database):
    async with db.read() as conn:
        return await engine_pool(conn, None)


async def test_engine_model_without_executor_model_serves_engine_not_executor(
    db, store
) -> None:
    acc = await custom(db, store, "or", "m/engine", None)
    assert (await lease(db, store, "engine")).account_id == acc
    assert (await pool(db)).usable == 1
    with pytest.raises(NoAccountError):
        await lease(db, store, "executor")


async def test_executor_model_without_engine_model_serves_executor_not_engine(
    db, store
) -> None:
    acc = await custom(db, store, "or", None, "m/executor")
    assert (await lease(db, store, "executor")).account_id == acc
    assert (await pool(db)).usable == 0  # the catalog agrees with the lease
    with pytest.raises(NoAccountError):
        await lease(db, store, "engine")


async def test_both_models_serve_both_roles(db, store) -> None:
    acc = await custom(db, store, "or", "m/engine", "m/executor")
    got = await lease(db, store, "engine")
    assert got.account_id == acc and got.settings.model == "m/engine"
    got = await lease(db, store, "executor", agent=B)
    assert got.account_id == acc and got.settings.model == "m/executor"


async def test_custom_without_any_model_serves_no_role(db, store) -> None:
    await custom(db, store, "legacy", None, None)
    for role in ("engine", "executor"):
        with pytest.raises(NoAccountError):
            await lease(db, store, role)
    assert (await pool(db)).usable == 0


async def test_custom_without_config_serves_no_role(db, store) -> None:
    acc = await custom(db, store, "or", "m/engine", "m/executor")
    async with db.write() as tx:
        await tx.conn.execute("UPDATE accounts SET config = NULL WHERE id = ?", (acc,))
    for role in ("engine", "executor"):
        with pytest.raises(NoAccountError):
            await lease(db, store, role)
    assert (await pool(db)).usable == 0


async def test_native_providers_without_config_are_unaffected(db, store) -> None:
    claude = await add(db, store, "claude", "anthropic")
    cursor = await add(db, store, "cursor", "cursor")
    assert (await lease(db, store, "engine")).account_id == claude
    # least leased wins the tie, and Cursor is a valid executor
    assert (await lease(db, store, "executor", agent=B)).account_id == cursor
    assert (await pool(db)).usable == 1  # cursor is never an engine


async def test_executor_skips_the_engine_only_custom_and_takes_the_next_global_rank(
    db, store
) -> None:
    await custom(db, store, "or", "m/engine", None, priority=100)
    claude = await add(db, store, "claude", "anthropic", priority=1)
    got = await lease(db, store, "executor")
    assert got.account_id == claude and got.provider == "anthropic"
    engine = await lease(db, store, "engine", agent=B)
    assert engine.provider == "custom"  # the higher priority still wins the engine


async def test_a_held_engine_only_custom_is_not_reused_as_the_executor(
    db, store
) -> None:
    acc = await custom(db, store, "or", "m/engine", None, priority=100, max_leases=1)
    engine = await lease(db, store, "engine")
    assert engine.account_id == acc
    # Without a compatible account the executor waits: no fallback onto the engine.
    with pytest.raises(NoAccountError):
        await lease(db, store, "executor", reuse_held=True)
    claude = await add(db, store, "claude", "anthropic")
    got = await lease(db, store, "executor", reuse_held=True)
    assert (got.account_id, got.role) == (claude, "executor")
    assert got.lease_id != engine.lease_id


async def test_a_held_custom_with_both_models_is_still_reused(db, store) -> None:
    acc = await custom(db, store, "or", "m/engine", "m/executor", max_leases=1)
    engine = await lease(db, store, "engine")
    got = await lease(db, store, "executor", reuse_held=True)
    assert (got.account_id, got.role) == (acc, "engine")
    assert got.lease_id == engine.lease_id


async def test_capacity_and_priority_still_order_the_roles_that_are_served(
    db, store
) -> None:
    hi = await custom(db, store, "hi", "m/e", "m/x", priority=10, max_leases=1)
    lo = await custom(db, store, "lo", "m/e", "m/x", priority=1)
    assert (await lease(db, store, "executor")).account_id == hi
    assert (await lease(db, store, "executor", agent=B)).account_id == lo


async def test_earliest_reset_ignores_an_exhausted_account_that_cannot_serve_the_role(
    db, store
) -> None:
    from datetime import timedelta

    acc = await custom(db, store, "or", "m/engine", None)
    async with db.write() as tx:
        until = tx.now + timedelta(hours=1)
        await tx.conn.execute(
            "UPDATE accounts SET state = 'exhausted', exhausted_until = ? WHERE id = ?",
            (until.isoformat(), acc),
        )
    async with db.write() as tx:
        assert await accounts.earliest_reset(tx, "executor", RUNTIME) is None
        assert await accounts.earliest_reset(tx, "engine", RUNTIME) is not None
