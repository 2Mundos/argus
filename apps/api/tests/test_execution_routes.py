"""Execution alternatives use disposable accounts and SQLite; no inference."""

import json
from pathlib import Path

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.secrets import MemorySecretStore
from argus_api.settings import service as settings
from argus_api.tasks import execution_routes as routes
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, AccountUpdate
from argus_core.execution_chains import EXECUTION_CAPABILITIES, ExecutionReport
from argus_core.tasks import TaskCreate

A = "agent-a"
CAPS = ["execution-chains-v1", *[c.id for c in EXECUTION_CAPABILITIES]]
PROVIDERS = ["local", "custom", "anthropic", "openai"]


def selector(account="local", provider="local", adapter="claude", model=None):
    return {
        "account_id": account,
        "provider": provider,
        "adapter": adapter,
        "model": model or {"source": "account"},
    }


def direct(id="direct", harness=None):
    return {
        "id": id,
        "mode": "single",
        "runtime": "nanoclaw",
        "harness": harness or selector(),
    }


def paired(id="paired"):
    return {
        **direct(id),
        "mode": "paired",
        "executor": selector(
            "custom", "custom", "opencode", {"source": "explicit", "id": "vendor/code"}
        ),
    }


@pytest.fixture
async def fleet(db):
    await insert_agent(db, A)
    store = MemorySecretStore()
    for id, provider, credential, config in [
        (
            "local",
            "local",
            {
                "base_url": "http://host.docker.internal:8080",
                "auth_token": "fixture",
                "model": "served-model",
            },
            None,
        ),
        (
            "custom",
            "custom",
            {"api_key": "fixture-key"},
            {
                "base_url": "https://openrouter.ai/api/v1",
                "label": "openrouter",
                "engine": {"model": "vendor/harness"},
                "executor": {"model": "vendor/code"},
            },
        ),
        ("cloud", "anthropic", {"api_key": "fixture-cloud"}, None),
    ]:
        await store.put(id, credential)
        async with db.write() as tx:
            await accounts.create_account(
                tx,
                id,
                AccountCreate(
                    name=id,
                    provider=provider,
                    secret_ref=f"sops:{id}" if provider != "local" else None,
                    credential=credential if provider == "local" else None,
                    config=config,
                    max_leases=2,
                ),
                f"sops:{id}",
                "admin:test",
                store,
            )
    return store


async def policy(db, alternatives, scope="global", difficulty="hard"):
    async with db.write() as tx:
        return await settings.put_setting(
            tx,
            Path(),
            scope,
            routes.KEY,
            {"version": 1, "difficulties": {difficulty: alternatives}},
            "admin:test",
        )


async def make_task(db, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx,
            TaskCreate(
                project="demo",
                title="route fixture",
                difficulty=kw.pop("difficulty", "hard"),
                **kw,
            ),
            "admin:test",
        )


async def claim(db, store, caps=CAPS):
    async with db.write() as tx:
        return await accounts.claim(tx, A, caps, PROVIDERS, store)


async def state(db, id):
    async with db.read() as conn:
        row = await tasks.task_row(conn, id)
        return dict(row), await routes.resolve(conn, row)


@pytest.mark.parametrize("difficulty", ["trivial", "simple", "standard", "hard"])
async def test_all_difficulties_direct_exact_account_without_executor(
    db, fleet, difficulty
):
    await policy(db, [direct()], difficulty=difficulty)
    async with db.write() as tx:
        task = await tasks.create_task(
            tx,
            TaskCreate(project="demo", title="direct", difficulty=difficulty),
            "admin:test",
        )
    claimed, lease = await claim(db, fleet)
    assert claimed.id == task.id and lease.account_id == "local"
    async with db.read() as conn:
        selection = await routes.current(conn, task.id, claimed.attempt)
        assert selection.executor is None and selection.harness.model == "served-model"
        async with conn.execute(
            "SELECT role FROM account_leases WHERE released_at IS NULL"
        ) as cur:
            assert [r[0] for r in await cur.fetchall()] == ["engine"]


async def test_atomic_second_lease_rollback_and_complete_fallback(db, fleet):
    await policy(db, [paired(), direct()])
    async with db.write() as tx:
        await accounts.update_account(
            tx, "custom", AccountUpdate(enabled=False), "admin:test"
        )
    task = await make_task(db)
    claimed, engine = await claim(db, fleet)
    row, diagnostic = await state(db, task.id)
    assert claimed and engine.account_id == "local"
    assert diagnostic.selection.alternative.id == "direct"
    async with db.read() as conn:
        async with conn.execute("SELECT count(*) FROM account_leases") as cur:
            assert (await cur.fetchone())[0] == 1
        async with conn.execute(
            "SELECT count(*) FROM events WHERE kind='account.leased'"
        ) as cur:
            assert (await cur.fetchone())[0] == 1
    assert row["failures"] == 0


@pytest.mark.parametrize("alternatives", [[paired(), direct()], [direct(), paired()]])
@pytest.mark.parametrize("difficulty", ["trivial", "simple", "standard", "hard"])
async def test_whole_alternative_fallback_durable_fenced_no_wrap(
    db, fleet, alternatives, difficulty
):
    await policy(db, alternatives, difficulty=difficulty)
    task = await make_task(db, difficulty=difficulty)
    claimed, _engine = await claim(db, fleet)
    _, diagnostic = await state(db, task.id)
    selection = diagnostic.selection
    async with db.write() as tx:
        await routes.report(
            tx,
            task.id,
            A,
            ExecutionReport(
                attempt=claimed.attempt,
                generation=selection.generation,
                action="applied",
            ),
        )
        await routes.report(
            tx,
            task.id,
            A,
            ExecutionReport(
                attempt=claimed.attempt,
                generation=selection.generation,
                action="advance",
                stopped=True,
                reason="quota",
            ),
        )
    with pytest.raises(ConflictError):
        async with db.write() as tx:
            await routes.report(
                tx,
                task.id,
                A,
                ExecutionReport(
                    attempt=claimed.attempt,
                    generation=selection.generation,
                    action="advance",
                    stopped=True,
                    reason="quota",
                ),
            )
    async with db.read() as conn, conn.execute("PRAGMA database_list") as cur:
        file = (await cur.fetchone())[2]
    reopened = await Database.open(Path(file))
    try:
        claimed, _engine = await claim(reopened, fleet)
    finally:
        await reopened.close()
    _, diagnostic = await state(db, task.id)
    assert diagnostic.selection.alternative.id == alternatives[1]["id"]
    assert diagnostic.selection.generation > selection.generation
    async with db.write() as tx:
        await routes.report(
            tx,
            task.id,
            A,
            ExecutionReport(
                attempt=claimed.attempt,
                generation=diagnostic.selection.generation,
                action="advance",
                stopped=True,
                reason="quota",
            ),
        )
    row, diagnostic = await state(db, task.id)
    assert row["state"] == "blocked" and row["block_reason"] == routes.EXHAUSTED
    assert row["failures"] == 0 and diagnostic.cursor == 2
    async with db.write() as tx:
        await tasks.unblock(tx, task.id, "admin:test")
    assert (await claim(db, fleet))[0] is None


async def test_inheritance_scoped_map_locks_and_pin_bypass(db, fleet, monkeypatch):
    await policy(db, [direct()])
    own = await policy(db, [paired()], "demo", "simple")
    assert set(own.value["difficulties"]) == {"simple", "hard"}
    assert set(own.scoped_value["difficulties"]) == {"simple"}
    task = await make_task(db, harness_account="cloud")
    _, route = await state(db, task.id)
    assert route.bypass == "legacy_task_pin"
    monkeypatch.setenv("ARGUS_MODEL_POLICY", "{}")
    with pytest.raises(ValidationFailedError, match="legacy_env_lock"):
        await policy(db, [direct()])


async def test_old_worker_cannot_claim_chain_and_missing_model_waits(db, fleet):
    await policy(db, [direct()])
    task = await make_task(db)
    assert (await claim(db, fleet, []))[0] is None
    await fleet.put(
        "local",
        {"base_url": "http://host.docker.internal:8080", "auth_token": "fixture"},
    )
    assert (await claim(db, fleet))[0] is None
    row, route = await state(db, task.id)
    assert row["attempt"] == row["failures"] == 0
    assert route.skips[0]["reason"] == "engine_model_unknown"


async def test_custom_pair_requires_separate_executor_lease(db, fleet):
    alternative = paired()
    alternative["harness"] = selector("custom", "custom", "claude")
    await policy(db, [alternative])
    task = await make_task(db)
    claimed, engine = await claim(db, fleet)
    _, route = await state(db, task.id)
    assert route.selection.executor.lease_id != engine.lease_id
    async with db.read() as conn:
        delivery = await routes.executor_delivery(conn, route.selection, A, fleet)
        assert delivery.role == "executor" and delivery.model == "vendor/code"
        attempts = await tasks.attempt_definitions(conn, task.id)
        assert "credential" not in json.dumps(attempts[0].definition["execution"])
    with pytest.raises(ConflictError, match="atomically"):
        async with db.write() as tx:
            await accounts.lease(
                tx,
                A,
                PROVIDERS,
                fleet,
                "executor",
                task_id=task.id,
                attempt=claimed.attempt,
            )


@pytest.mark.parametrize("mode", ["single", "paired"])
async def test_native_exact_same_account_and_custom_capacity(db, fleet, mode):
    native = direct(
        "native",
        selector(
            "cloud",
            "anthropic",
            "claude",
            {"source": "explicit", "id": "fixture/harness"},
        ),
    )
    if mode == "paired":
        native.update(
            mode="paired",
            executor=selector(
                "cloud",
                "anthropic",
                "claude",
                {"source": "explicit", "id": "fixture/code"},
            ),
        )
    await policy(db, [native])
    task = await make_task(db)
    claimed, engine = await claim(db, fleet)
    _, route = await state(db, task.id)
    assert claimed.id == task.id and engine.account_id == "cloud"
    assert route.selection.harness.model == "fixture/harness"
    if mode == "paired":
        assert route.selection.executor.lease_id == engine.lease_id
        assert route.selection.executor.model == "fixture/code"
    else:
        assert route.selection.executor is None


async def test_custom_same_account_needs_two_slots_or_no_claim(db, fleet):
    pair = paired()
    pair["harness"] = selector("custom", "custom", "claude")
    await policy(db, [pair])
    async with db.write() as tx:
        await accounts.update_account(
            tx, "custom", AccountUpdate(max_leases=1), "admin:test"
        )
    task = await make_task(db)
    assert (await claim(db, fleet))[0] is None
    row, _ = await state(db, task.id)
    assert row["attempt"] == row["failures"] == 0
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) FROM account_leases") as cur,
    ):
        assert (await cur.fetchone())[0] == 0


async def test_reset_generation_fencing_stop_and_exact_release(db, fleet):
    await policy(db, [direct(), paired()])
    task = await make_task(db)
    claimed, engine = await claim(db, fleet)
    _, route = await state(db, task.id)
    first = route.selection
    with pytest.raises(ValidationFailedError, match="stop"):
        async with db.write() as tx:
            await routes.report(
                tx,
                task.id,
                A,
                ExecutionReport(
                    attempt=claimed.attempt,
                    generation=first.generation,
                    action="advance",
                    reason="quota",
                ),
            )
    async with db.write() as tx:
        # An unrelated lease must survive; release is by tuple ID, not agent/role.
        other = await accounts._try_lease(
            tx, A, fleet, "executor", ["custom"], only_account="custom"
        )
        await routes.report(
            tx,
            task.id,
            A,
            ExecutionReport(
                attempt=claimed.attempt,
                generation=first.generation,
                action="advance",
                stopped=True,
                reason="quota",
            ),
        )
    async with (
        db.read() as conn,
        conn.execute("SELECT id FROM account_leases WHERE released_at IS NULL") as cur,
    ):
        assert [r[0] for r in await cur.fetchall()] == [other.lease_id]
    async with db.write() as tx:
        await accounts.release(tx, A, "fixture_teardown")
        await routes.reset(tx, task.id, "admin:test")
    next_claim, _ = await claim(db, fleet)
    _, route = await state(db, task.id)
    assert route.selection.generation > first.generation
    assert route.selection.index == 0
    with pytest.raises(ConflictError):
        async with db.write() as tx:
            await routes.report(
                tx,
                task.id,
                A,
                ExecutionReport(
                    attempt=claimed.attempt,
                    generation=first.generation,
                    action="advance",
                    stopped=True,
                    reason="quota",
                ),
            )
    async with db.write() as tx:
        await routes.check_generation(
            tx, task.id, next_claim.attempt, route.selection.generation
        )
    with pytest.raises(ConflictError):
        async with db.write() as tx:
            await routes.check_generation(
                tx, task.id, next_claim.attempt, first.generation
            )
    assert engine.lease_id != route.selection.harness.lease_id


async def test_unrelated_difficulty_edit_keeps_cursor_and_ordinary_failure(db, fleet):
    await policy(db, [direct(), paired()])
    task = await make_task(db)
    claimed, _ = await claim(db, fleet)
    _, route = await state(db, task.id)
    first = route.selection
    async with db.write() as tx:
        await routes.report(
            tx,
            task.id,
            A,
            ExecutionReport(
                attempt=claimed.attempt,
                generation=first.generation,
                action="advance",
                stopped=True,
                reason="quota",
            ),
        )
        await settings.put_setting(
            tx,
            Path(),
            "global",
            routes.KEY,
            {
                "version": 1,
                "difficulties": {"hard": [direct(), paired()], "simple": [direct()]},
            },
            "admin:test",
        )
    claimed, _ = await claim(db, fleet)
    _, route = await state(db, task.id)
    assert route.selection.index == 1 and route.selection.revision == first.revision
    async with db.write() as tx:
        await tasks.fail(
            tx, A, task.id, claimed.attempt, "implementation error", "worker_fail"
        )
        await accounts.release(tx, A, "fixture_runtime_stopped")
    _, route = await state(db, task.id)
    assert route.cursor == 1


async def test_capability_and_provider_restrictions_never_use_hidden_legacy(db, fleet):
    await policy(db, [paired()])
    task = await make_task(db, account_providers=["local"])
    caps = [c for c in CAPS if c != "nanoclaw:executor:opencode:custom"]
    assert (await claim(db, fleet, caps))[0] is None
    row, route = await state(db, task.id)
    assert (
        row["attempt"] == 0 and route.skips[0]["reason"] == "worker_capability_missing"
    )
    async with db.write() as tx:
        await tasks.unblock(tx, task.id, "admin:test")
    claimed, _ = await claim(db, fleet)
    assert claimed.id == task.id


async def test_reference_validation_and_role_change(db, fleet):
    with pytest.raises(ValidationFailedError):
        await policy(db, [direct(harness=selector("missing"))])
    with pytest.raises(ValidationFailedError):
        await policy(db, [direct(harness=selector("cloud"))])
    await policy(db, [paired()])
    task = await make_task(db)
    async with db.write() as tx:
        await accounts.update_account(
            tx,
            "custom",
            AccountUpdate(
                config={
                    "base_url": "https://openrouter.ai/api/v1",
                    "label": "openrouter",
                    "engine": {"model": "fixture/harness"},
                }
            ),
            "admin:test",
        )
    assert (await claim(db, fleet))[0] is None
    row, route = await state(db, task.id)
    assert (
        row["attempt"] == 0
        and route.skips[0]["reason"] == "executor_account_unavailable"
    )
