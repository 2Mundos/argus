"""Per-task harness and executor model pins (docs/specs/task-models.spec.md)."""

import json

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate
from argus_core.task_edit import TaskChanges
from argus_core.tasks import ExecutorModel, HarnessModel, TaskCreate

A = "agent-a"
RUNTIME = ["anthropic", "openai", "cursor"]
OPUS = HarnessModel(engine="claude", model="claude-opus-5-5")
SOL = ExecutorModel(executor="codex", model="gpt-6.1-sol")


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def add_account(db: Database, store, name: str, provider: str, priority: int):
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider=provider,
                secret_ref=f"sops:{name}",
                priority=priority,
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )


async def make_task(db: Database, **kw) -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="global", title="pinned", **kw), "admin:test"
        )
    return task.id


async def claim(db: Database):
    async with db.write() as tx:
        return await tasks.claim(tx, A, [], runtime_providers=RUNTIME)


def test_pins_are_validated() -> None:
    for bad in (
        {"harness_model": {"engine": "agent", "model": "x"}},
        {"harness_model": {"engine": "claude", "model": "-bad"}},
        {"harness_model": {"engine": "claude"}},
        {"executor_model": {"executor": "copilot", "model": "x"}},
        {"executor_model": {"executor": "codex", "model": "x", "extra": 1}},
        # the pinned engine's provider is excluded by the restriction
        {"harness_model": OPUS, "account_providers": ["openai"]},
    ):
        with pytest.raises(ValueError):
            TaskCreate(project="global", title="x", **bad)
    ok = TaskCreate(
        project="global", title="x", harness_model=OPUS, account_providers=["anthropic"]
    )
    assert ok.harness_model == OPUS


async def test_harness_pin_narrows_the_engine_lease(db: Database, store, agent) -> None:
    await add_account(db, store, "codex", "openai", priority=100)
    await add_account(db, store, "claude", "anthropic", priority=0)
    await make_task(db, harness_model=OPUS, executor_model=SOL)
    task = await claim(db)
    assert task is not None
    assert task.harness_model == OPUS and task.executor_model == SOL
    async with db.write() as tx:
        engine = await accounts.lease(
            tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )
        executor = await accounts.lease(
            tx, A, RUNTIME, store, "executor", task_id=task.id, attempt=task.attempt
        )
    assert engine.provider == "anthropic"
    assert executor.provider == "openai"


async def test_executor_pin_narrows_the_executor_lease(
    db: Database, store, agent
) -> None:
    await add_account(db, store, "claude", "anthropic", priority=100)
    await add_account(db, store, "cursor", "cursor", priority=0)
    await make_task(
        db, executor_model=ExecutorModel(executor="agent", model="composer-2.5")
    )
    task = await claim(db)
    assert task is not None
    async with db.write() as tx:
        executor = await accounts.lease(
            tx, A, RUNTIME, store, "executor", task_id=task.id, attempt=task.attempt
        )
    assert executor.provider == "cursor"


async def test_opencode_pin_narrows_executor_lease_to_local(
    db: Database, store, agent
) -> None:
    await add_account(db, store, "claude", "anthropic", priority=100)
    local_cred = {
        "base_url": "http://127.0.0.1:8080",
        "auth_token": "argus-local-harness",
        "model": "qwen3",
    }
    await store.put("strata", local_cred)
    async with db.write() as tx:
        await accounts.create_account(
            tx,
            "strata",
            AccountCreate(
                name="strata",
                provider="local",
                credential=local_cred,
                priority=0,
            ),
            "sops:strata",
            "admin:test",
            store,
        )
    await make_task(
        db, executor_model=ExecutorModel(executor="opencode", model="qwen3")
    )
    task = await claim(db)
    assert task is not None
    async with db.write() as tx:
        executor = await accounts.lease(
            tx,
            A,
            [*RUNTIME, "local"],
            store,
            "executor",
            task_id=task.id,
            attempt=task.attempt,
        )
    assert executor.provider == "local"
    assert executor.settings is not None
    assert executor.settings.label == "local"
    assert executor.settings.model == "qwen3"


async def test_harness_pin_yields_to_another_provider(
    db: Database, store, agent
) -> None:
    await add_account(db, store, "codex", "openai", priority=100)
    task_id = await make_task(db, harness_model=OPUS)
    task = await claim(db)  # only Codex is usable: the pin is a preference
    assert task is not None and task.id == task_id
    async with db.write() as tx:
        engine = await accounts.lease(
            tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )
    assert engine.provider == "openai"


async def test_pins_edit_rules(db: Database, store, agent) -> None:
    await add_account(db, store, "claude", "anthropic", priority=0)
    task_id = await make_task(db, account_providers=["openai"])
    async with db.read() as conn:
        version = (await tasks.get_task(conn, task_id)).version
    # checked against the merged task: the stored restriction excludes Claude
    with pytest.raises(ValidationFailedError, match="excluded by account_providers"):
        async with db.write() as tx:
            await tasks.edit_task(
                tx, task_id, TaskChanges(harness_model=OPUS), version, "admin:t"
            )
    async with db.write() as tx:
        edited = await tasks.edit_task(
            tx,
            task_id,
            TaskChanges(harness_model=OPUS, executor_model=SOL, account_providers=None),
            version,
            "admin:t",
        )
    assert edited.harness_model == OPUS and edited.executor_model == SOL
    async with (
        db.read() as conn,
        conn.execute("SELECT data FROM events WHERE kind = 'task.edited'") as cur,
    ):
        changes = json.loads((await cur.fetchone())["data"])["changes"]
    assert changes["harness_model"] == {"from": None, "to": OPUS.model_dump()}
    async with db.write() as tx:
        cleared = await tasks.edit_task(
            tx, task_id, TaskChanges(executor_model=None), edited.version, "admin:t"
        )
    assert cleared.executor_model is None and cleared.harness_model == OPUS
    claimed = await claim(db)
    assert claimed is not None
    async with db.read() as conn:
        attempts = await tasks.attempt_definitions(conn, task_id)
    assert attempts[0].definition["harness_model"] == OPUS.model_dump()
    with pytest.raises(ConflictError):
        async with db.write() as tx:
            await tasks.edit_task(
                tx, task_id, TaskChanges(harness_model=None), claimed.version, "admin:t"
            )
