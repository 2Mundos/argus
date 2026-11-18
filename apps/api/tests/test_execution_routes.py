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


async def policy(db, alternatives, scope="global", difficulty="hard", key=routes.KEY):
    async with db.write() as tx:
        return await settings.put_setting(
            tx,
            Path(),
            scope,
            key,
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
    for key in (routes.KEY, routes.REVIEW_KEY):
        with pytest.raises(ValidationFailedError, match="legacy_env_lock"):
            await policy(db, [direct()], key=key)


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


async def test_local_harness_with_opencode_custom_executor(db, fleet):
    """A local direct route pairs with the scoped OpenCode/custom lane, two leases."""
    await policy(db, [paired()])
    task = await make_task(db)
    claimed, engine = await claim(db, fleet)
    _, route = await state(db, task.id)
    assert claimed.id == task.id
    assert engine.account_id == "local" and engine.role == "engine"
    assert route.selection.harness.model == "served-model"
    assert route.selection.executor.selector.adapter == "opencode"
    assert route.selection.executor.selector.provider == "custom"
    assert route.selection.executor.account_id == "custom"
    assert route.selection.executor.lease_id != engine.lease_id
    async with db.read() as conn:
        delivery = await routes.executor_delivery(conn, route.selection, A, fleet)
    assert delivery.role == "executor" and delivery.provider == "custom"
    assert delivery.model == "vendor/code"
    assert delivery.settings.model == "vendor/code"
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT role FROM account_leases WHERE released_at IS NULL ORDER BY role"
        ) as cur,
    ):
        assert [r[0] for r in await cur.fetchall()] == ["engine", "executor"]


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
    for key in (routes.KEY, routes.REVIEW_KEY):
        with pytest.raises(ValidationFailedError):
            await policy(db, [direct(harness=selector("missing"))], key=key)
        with pytest.raises(ValidationFailedError):
            await policy(db, [direct(harness=selector("cloud"))], key=key)
        with pytest.raises(ValidationFailedError, match="duplicate_alternative_id"):
            await policy(db, [direct(), direct()], key=key)
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


async def add_account(db, store, id, provider, credential, config=None, **extra):
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
                **extra,
            ),
            f"sops:{id}",
            "admin:test",
            store,
        )


def anyone(provider="local", adapter="claude", model=None):
    value = selector(provider=provider, adapter=adapter, model=model)
    del value["account_id"]
    return value


async def test_unpinned_pair_resolves_concrete_accounts_and_records_them(db, fleet):
    # A custom account without an executor model cannot serve the role: skipped.
    await add_account(
        db,
        fleet,
        "custom-engine-only",
        "custom",
        {"api_key": "k"},
        {
            "base_url": "https://openrouter.ai/api/v1",
            "label": "openrouter",
            "engine": {"model": "vendor/harness"},
        },
        priority=100,
    )
    pair = paired()
    pair["harness"] = anyone()
    pair["executor"] = anyone(
        "custom", "opencode", {"source": "explicit", "id": "vendor/code"}
    )
    await policy(db, [pair])
    task = await make_task(db)
    claimed, engine = await claim(db, fleet)
    assert claimed.id == task.id and engine.account_id == "local"
    _, route = await state(db, task.id)
    assert route.selection.harness.account_id == "local"
    assert route.selection.executor.account_id == "custom"
    assert route.selection.harness.selector.account_id is None
    async with db.read() as conn:
        # Auditable on the attempt record and in the selection event.
        recorded = await routes.current(conn, task.id, claimed.attempt)
        assert recorded.executor.account_id == "custom"
        async with conn.execute(
            "SELECT data FROM events WHERE kind='task.execution_route_selected'"
        ) as cur:
            data = json.loads((await cur.fetchone())[0])
        assert data["executor"]["account_id"] == "custom"


async def test_unpinned_single_local_route_needs_no_account(db, fleet):
    await policy(db, [direct(harness=anyone())], difficulty="trivial")
    task = await make_task(db, difficulty="trivial")
    claimed, engine = await claim(db, fleet)
    assert claimed.id == task.id and engine.account_id == "local"


async def test_unpinned_selector_skips_account_not_serving_a_model(db, fleet):
    await add_account(
        db,
        fleet,
        "local-b",
        "local",
        {"base_url": "http://host.docker.internal:8081", "auth_token": "x"},
        priority=100,
    )
    await policy(db, [direct(harness=anyone())])
    task = await make_task(db)
    # local-b ranks first but names no model and serves two; local serves its own.
    served = {"local-b": ("m1", "m2"), "local": ("served-model",)}
    async with db.write() as tx:
        claimed, engine = await accounts.claim(
            tx, A, CAPS, PROVIDERS, fleet, local_served_models=served
        )
    assert claimed.id == task.id and engine.account_id == "local"
    assert (await state(db, task.id))[1].selection.harness.model == "served-model"


@pytest.mark.parametrize("pinned_side", ["harness", "executor"])
async def test_unpinned_native_pair_adopts_the_pinned_side(db, fleet, pinned_side):
    await add_account(db, fleet, "cloud-b", "anthropic", {"api_key": "k"}, priority=100)
    pin = selector(
        "cloud-b", "anthropic", "claude", {"source": "explicit", "id": "fixture/h"}
    )
    free = anyone("anthropic", "claude", {"source": "explicit", "id": "fixture/x"})
    sides = {"harness": pin, "executor": free}
    if pinned_side == "executor":
        sides = {"harness": free, "executor": pin}
    await policy(db, [{**direct("native"), "mode": "paired", **sides}])
    task = await make_task(db)
    claimed, engine = await claim(db, fleet)
    assert claimed.id == task.id and engine.account_id == "cloud-b"
    _, route = await state(db, task.id)
    assert route.selection.executor.lease_id == engine.lease_id


async def test_pinned_pair_keeps_working_and_unpinned_default_is_unchanged(db, fleet):
    # No chain stored: legacy routing, exactly as before.
    task = await make_task(db)
    _, route = await state(db, task.id)
    assert route is None
    await policy(db, [paired()])
    claimed, engine = await claim(db, fleet)
    assert claimed.id == task.id and engine.account_id == "local"
    assert (await state(db, task.id))[1].selection.executor.account_id == "custom"


# Independent reviewers on `review.chains` (settings.spec.md 3.5).

OPUS = {"source": "explicit", "id": "claude-opus-5-5"}


def explicit(id, provider, adapter, model, account=None):
    harness = anyone(provider, adapter, {"source": "explicit", "id": model})
    if account:
        harness["account_id"] = account
    return direct(id, harness)


OPUS_ALT = explicit("opus", "anthropic", "claude", "claude-opus-5-5")
SONNET_ALT = explicit("sonnet", "anthropic", "claude", "claude-sonnet-5-5")
CODEX_ALT = explicit("codex", "openai", "codex", "gpt-5.5")


async def reviewed_parent(db, store, *executions):
    """An in_review hard parent whose model_executions are (model, provider, account)."""
    await add_account(db, store, "codex", "openai", {"api_key": "fixture-openai"})
    parent = await make_task(db)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET state = 'in_review' WHERE id = ?", (parent.id,)
        )
        await tx.conn.execute(
            "UPDATE agents SET capabilities = ?, runtime_providers = ? WHERE id = ?",
            (json.dumps(CAPS), json.dumps(PROVIDERS), A),
        )
        for i, (model, provider, account) in enumerate(executions):
            await record_execution(tx, parent.id, i, model, provider, account)
    return parent


async def record_execution(tx, task_id, key, model, provider, account):
    await tx.conn.execute(
        "INSERT INTO model_executions (id, task_id, key, attempt, layer, state,"
        " provider, configured_model, account_id, recorded_at, updated_at)"
        " VALUES (?, ?, ?, 1, 'harness', 'succeeded', ?, ?, ?, ?, ?)",
        (
            f"{task_id}-{key}",
            task_id,
            str(key),
            provider,
            model,
            account,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )


async def review_policy(db, alternatives, difficulty="hard"):
    await policy(db, alternatives, difficulty=difficulty, key=routes.REVIEW_KEY)


async def spawn(db, parent_id, mode="different_model", policy=None, **kw):
    from argus_api.tasks import independent_review as review
    from argus_core.settings import IndependentReviewSetting

    async with db.write() as tx:
        return await review.spawn_reviewer(
            tx,
            await tasks.task_row(tx.conn, parent_id),
            pr=None,
            head_sha="abc1234",
            setting=IndependentReviewSetting(mode=mode),
            policy=policy or {},
            actor="system",
            **kw,
        )


async def parent_events(db, parent_id, kind):
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = ? ORDER BY rowid",
            (parent_id, kind),
        ) as cur,
    ):
        return [json.loads(r[0]) for r in await cur.fetchall()]


async def test_review_chain_skips_author_and_child_runs_on_fallback(db, fleet):
    await review_policy(db, [OPUS_ALT, CODEX_ALT])
    # The generic chain for every level must not re-route the reviewer.
    async with db.write() as tx:
        await settings.put_setting(
            tx,
            Path(),
            "global",
            routes.KEY,
            {
                "version": 1,
                "difficulties": {
                    d: [direct()] for d in ("trivial", "simple", "standard", "hard")
                },
            },
            "admin:test",
        )
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    child_id = await spawn(db, parent.id)
    [selected] = await parent_events(db, parent.id, "review.independent_selected")
    assert selected["selected"] == "codex" and selected["fallbacks"] == []
    assert selected["skipped"] == [{"id": "opus", "reason": "author"}]
    row, route = await state(db, child_id)
    assert row["harness_model"] is None and row["executor_model"] is None
    assert route.source == "review" and route.bypass is None
    assert [a.id for a in route.alternatives] == ["codex"]
    assert route.review.selected == "codex" and route.review.parent_id == parent.id
    claimed, engine = await claim(db, fleet)
    assert claimed.id == child_id and engine.account_id == "codex"
    _, route = await state(db, child_id)
    assert route.selection.source == "review"
    assert route.selection.harness.model == "gpt-5.5"


async def test_review_chain_of_only_authors_blocks(db, fleet):
    await review_policy(db, [OPUS_ALT, direct()])
    # A local account whose model is unknown until lease authored the parent.
    parent = await reviewed_parent(
        db,
        fleet,
        ("claude-opus-5-5", "anthropic", "cloud"),
        ("served-model", "local", "local"),
    )
    assert await spawn(db, parent.id) is None
    row, _ = await state(db, parent.id)
    assert row["block_reason"] == "no_independent_reviewer"
    [blocked] = await parent_events(db, parent.id, "review.independent_blocked")
    assert blocked["skipped"] == [
        {"id": "opus", "reason": "author"},
        {"id": "direct", "reason": "author"},
    ]
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM tasks WHERE parent_id = ?", (parent.id,)
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 0


@pytest.mark.parametrize(
    ("mode", "chosen"), [("different_model", "sonnet"), ("different_provider", "codex")]
)
async def test_review_chain_different_provider_skips_by_provider(
    db, fleet, mode, chosen
):
    await review_policy(db, [OPUS_ALT, SONNET_ALT, CODEX_ALT])
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    child_id = await spawn(db, parent.id, mode=mode)
    _, route = await state(db, child_id)
    assert route.review.selected == chosen


async def test_unconfigured_review_level_uses_policy_picker(db, fleet):
    await review_policy(db, [CODEX_ALT], difficulty="simple")
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    policy = {"harness": {"claude": ["claude-opus-5-5", "claude-sonnet-5-5"]}}
    child_id = await spawn(db, parent.id, policy=policy)
    row, _ = await state(db, child_id)
    assert row["execution_route"] is None
    assert json.loads(row["harness_model"]) == {
        "engine": "claude",
        "model": "claude-sonnet-5-5",
    }
    assert await parent_events(db, parent.id, "review.independent_selected") == []


async def test_review_chain_falls_back_past_unusable_account(db, fleet):
    pinned = explicit("sonnet", "anthropic", "claude", "claude-sonnet-5-5", "cloud")
    await review_policy(db, [pinned, CODEX_ALT])
    parent = await reviewed_parent(db, fleet, ("served-model", "local", "local"))
    async with db.write() as tx:
        await accounts.update_account(
            tx, "cloud", AccountUpdate(enabled=False), "admin:test"
        )
    child_id = await spawn(db, parent.id)
    _, route = await state(db, child_id)
    assert route.review.selected == "codex"
    assert [s.model_dump() for s in route.review.skipped] == [
        {"id": "sonnet", "reason": "unavailable"}
    ]


async def test_void_review_reselects_without_the_void_model(db, fleet):
    from argus_api.tasks import independent_review as review
    from argus_core.settings import IndependentReviewSetting
    from argus_core.tasks import TaskResult

    await review_policy(db, [CODEX_ALT, SONNET_ALT])
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    first = await spawn(db, parent.id)
    async with db.write() as tx:
        # The reviewer delegated to the author model: its verdict proves nothing.
        await record_execution(tx, first, 0, "gpt-5.5", "openai", "codex")
        await record_execution(tx, first, 1, "claude-opus-5-5", "anthropic", "cloud")
        await review.on_review_child_complete(
            tx,
            await tasks.task_row(tx.conn, first),
            TaskResult(
                summary="ok", data={"verdict": "approve", "head_sha": "abc1234"}
            ),
            setting=IndependentReviewSetting(mode="different_model"),
            policy={},
            actor="system",
        )
    assert await parent_events(db, parent.id, "review.independent_void")
    _, second = await parent_events(db, parent.id, "review.independent_selected")
    assert second["selected"] == "sonnet"
    assert second["skipped"] == [{"id": "codex", "reason": "author"}]


# A reviewer child that ends without a verdict (settings.spec.md 3.5).


async def review_setting(db, **value):
    async with db.write() as tx:
        await settings.put_setting(
            tx,
            Path(),
            "global",
            "review.independent",
            {"mode": "different_model", "max_rounds": 3, **value},
            "admin:test",
        )


async def cancel_child(db, child_id):
    async with db.write() as tx:
        await tasks.cancel(tx, child_id, "admin:test")


async def latest_child(db, parent_id):
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT id FROM tasks WHERE parent_id = ? ORDER BY rowid DESC",
            (parent_id,),
        ) as cur,
    ):
        return (await cur.fetchone())[0]


async def open_decisions(db, parent_id):
    from argus_api.decisions import store

    async with db.read() as conn:
        ids = await store.open_ids(conn, "task", parent_id)
        return [await store.get(conn, i) for i in ids]


async def answer(db, decision_id, choice, text=""):
    from argus_api.auth import Identity
    from argus_api.decisions import service as decisions
    from argus_core.auth import Scope
    from argus_core.decisions import DecisionAnswer

    async with db.write() as tx:
        return await decisions.answer(
            tx,
            decision_id,
            DecisionAnswer(choice=choice, text=text),
            Identity("t-admin", Scope.ADMIN, None, "test"),
        )


async def exhausted_parent(db, fleet):
    """A parent whose only reviewer alternative failed with the bound at zero."""
    await review_policy(db, [CODEX_ALT, SONNET_ALT])
    await review_setting(db, max_reviewer_failures=0)
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    first = await spawn(db, parent.id)
    await cancel_child(db, first)
    return parent, first


async def test_failed_reviewer_child_reselects_the_next_alternative(db, fleet):
    await review_policy(db, [CODEX_ALT, SONNET_ALT])
    await review_setting(db)
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    first = await spawn(db, parent.id)
    await cancel_child(db, first)
    [failed] = await parent_events(db, parent.id, "review.independent_child_failed")
    assert failed["child_id"] == first and failed["state"] == "cancelled"
    assert failed["alternative"] == "codex"
    _, second = await parent_events(db, parent.id, "review.independent_selected")
    assert second["selected"] == "sonnet"
    assert second["skipped"] == [{"id": "codex", "reason": "failed"}]
    row, _ = await state(db, parent.id)
    assert row["block_reason"] is None
    assert await latest_child(db, parent.id) != first
    # A repeated callback for the same child changes nothing.
    async with db.write() as tx:
        from argus_api.tasks import independent_review as review
        from argus_core.tasks import TaskState

        await review.on_review_child_ended(
            tx, await tasks.task_row(tx.conn, first), TaskState.CANCELLED, "system"
        )
    assert (
        len(await parent_events(db, parent.id, "review.independent_child_failed")) == 1
    )
    assert len(await parent_events(db, parent.id, "review.independent_selected")) == 2


async def test_failed_state_records_error_class_account_and_model(db, fleet):
    from argus_api.tasks import independent_review as review
    from argus_core.tasks import TaskState

    await review_policy(db, [CODEX_ALT, SONNET_ALT])
    await review_setting(db)
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    first = await spawn(db, parent.id)
    async with db.write() as tx:
        await record_execution(tx, first, 0, "gpt-5.5", "openai", "codex")
        await tx.conn.execute(
            "UPDATE tasks SET state = 'failed', error = ? WHERE id = ?",
            ("401 Unauthorized: login expired", first),
        )
        await review.on_review_child_ended(
            tx, await tasks.task_row(tx.conn, first), TaskState.FAILED, "system"
        )
    [failed] = await parent_events(db, parent.id, "review.independent_child_failed")
    assert failed["account"] == "codex" and failed["model"] == "gpt-5.5"
    assert failed["error_class"] and failed["state"] == "failed"
    async with db.read() as conn:
        [summary] = await review.failed_reviewer_summaries(conn, parent.id)
    assert summary.child_task_id == first and summary.account == "codex"


async def test_bound_reached_blocks_review_failed_with_one_decision(db, fleet):
    parent, first = await exhausted_parent(db, fleet)
    row, _ = await state(db, parent.id)
    assert row["state"] == "in_review" and row["block_reason"] == "review_failed"
    [decision] = await open_decisions(db, parent.id)
    assert decision.options == ["retry_reviewer", "approve_without_review", "cancel"]
    assert decision.payload["failed"][0]["child_id"] == first
    # No second child and no author model was ever selected.
    assert await latest_child(db, parent.id) == first
    assert len(await parent_events(db, parent.id, "review.independent_selected")) == 1


async def test_no_alternative_left_blocks_review_failed(db, fleet):
    await review_policy(db, [OPUS_ALT, CODEX_ALT])
    await review_setting(db)
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    first = await spawn(db, parent.id)
    await cancel_child(db, first)
    row, _ = await state(db, parent.id)
    assert row["block_reason"] == "review_failed"
    [blocked] = await parent_events(db, parent.id, "review.independent_blocked")
    assert blocked["reason"] == "review_failed"
    assert blocked["skipped"] == [
        {"id": "opus", "reason": "author"},
        {"id": "codex", "reason": "failed"},
    ]
    assert len(await open_decisions(db, parent.id)) == 1


async def test_retry_reviewer_spawns_a_new_child(db, fleet):
    parent, first = await exhausted_parent(db, fleet)
    [decision] = await open_decisions(db, parent.id)
    await answer(db, decision.id, "retry_reviewer")
    second = await latest_child(db, parent.id)
    assert second != first
    row, _ = await state(db, parent.id)
    assert row["state"] == "in_review" and row["block_reason"] is None
    assert not await open_decisions(db, parent.id)
    # The failure window restarts: the retried reviewer gets the full chain again.
    _, retried = await parent_events(db, parent.id, "review.independent_selected")
    assert retried["selected"] == "codex"
    # Its own failure counts against a fresh bound, so it escalates again.
    await cancel_child(db, second)
    assert len(await open_decisions(db, parent.id)) == 1


async def test_approve_without_review_completes_the_parent_with_audit(db, fleet):
    parent, _ = await exhausted_parent(db, fleet)
    [decision] = await open_decisions(db, parent.id)
    await answer(db, decision.id, "approve_without_review", "reviewer logins down")
    row, _ = await state(db, parent.id)
    assert row["state"] == "succeeded" and row["block_reason"] is None
    [waived] = await parent_events(db, parent.id, "review.independent_waived")
    assert waived["decision_id"] == decision.id
    assert waived["notes"] == "reviewer logins down" and len(waived["failed"]) == 1


async def test_cancel_option_cancels_the_parent(db, fleet):
    parent, _ = await exhausted_parent(db, fleet)
    [decision] = await open_decisions(db, parent.id)
    await answer(db, decision.id, "cancel")
    row, _ = await state(db, parent.id)
    assert row["state"] == "cancelled"


async def test_review_failed_decision_refuses_plain_review_choices(db, fleet):
    parent, _ = await exhausted_parent(db, fleet)
    [decision] = await open_decisions(db, parent.id)
    with pytest.raises(ValidationFailedError):
        await answer(db, decision.id, "accept")


async def test_auth_expired_advances_chain_blocks_account_and_spends_no_failure(
    db, fleet
):
    second = direct("second", selector("custom", "custom", "claude"))
    await policy(db, [direct(), second])
    task = await make_task(db)
    claimed, _engine = await claim(db, fleet)
    _, diagnostic = await state(db, task.id)
    assert diagnostic.selection.harness.account_id == "local"
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
                reason="auth_expired",
            ),
        )
    row, _ = await state(db, task.id)
    assert row["state"] == "ready" and row["failures"] == 0
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM events WHERE kind='account.auth_expired'"
            " AND subject_id='local'"
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 1
    claimed, engine = await claim(db, fleet)
    _, diagnostic = await state(db, task.id)
    assert engine.account_id == "custom" and claimed.failures == 0
    assert diagnostic.selection.alternative.id == "second"
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
                reason="auth_expired",
            ),
        )
    row, diagnostic = await state(db, task.id)
    assert row["state"] == "blocked" and row["block_reason"] == routes.EXHAUSTED
    assert row["failures"] == 0
