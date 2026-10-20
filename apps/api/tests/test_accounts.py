import asyncio
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api.accounts import service as accounts
from argus_api.accounts.service import NoAccountError
from argus_api.db import Database, ts
from argus_api.errors import ConflictError, ForbiddenError
from argus_api.main import app
from argus_api.reaper import reap
from argus_api.secrets import MemorySecretStore, SecretStoreError
from argus_api.tasks import service as tasks
from argus_core.accounts import (
    AccountCreate,
    AccountState,
    AccountUpdate,
    ExhaustedReport,
    UsageReport,
)
from argus_core.tasks import TaskCreate, TaskResult, Usage
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient

A, B = "agent-a", "agent-b"


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)


async def add(
    db: Database, store: MemorySecretStore, name: str, provider: str, **kw
) -> str:
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


async def lease(db: Database, store, agent=A, runtime=None):
    async with db.write() as tx:
        return await accounts.lease(tx, agent, runtime, store)


async def get(db: Database, account_id: str, clock: FakeClock):
    async with db.read() as conn:
        return await accounts.get_account(conn, account_id, clock.now)


async def exhaust(db: Database, store, account_id: str, agent=A, **kw):
    async with db.write() as tx:
        return await accounts.exhausted(
            tx, agent, account_id, ExhaustedReport(**kw), store
        )


# -- leasing --------------------------------------------------------------------------


async def test_lease_ranks_globally_by_priority_then_deterministic_ties(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "openai-1", "openai", priority=5)
    await add(db, store, "claude-1", "anthropic", priority=5)
    await add(db, store, "claude-low", "anthropic", priority=0)
    got = await lease(db, store)
    assert got.name == "claude-1"  # tie on priority and load: provider, name
    assert got.credential == {"api_key": "key-claude-1"}


async def test_higher_priority_beats_lower_across_providers(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "codex-1", "openai", priority=10)
    await add(db, store, "cursor-1", "cursor", priority=10)
    await add(db, store, "claude-1", "anthropic", priority=100)
    await add(db, store, "claude-2", "anthropic", priority=100)
    assert (await lease(db, store, A)).name == "claude-1"
    assert (await lease(db, store, B)).name == "claude-2"  # least leased tie-break


async def test_unusable_accounts_are_skipped_in_priority_order(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    disabled = await add(db, store, "claude-off", "anthropic", priority=100)
    async with db.write() as tx:
        await accounts.update_account(
            tx, disabled, AccountUpdate(enabled=False), "admin:test"
        )
    full = await add(db, store, "claude-full", "anthropic", priority=99, max_leases=1)
    await add(db, store, "claude-out", "anthropic", priority=98)
    await store.put("claude-nocred", {"api_key": "x"})
    nocred = await add(db, store, "claude-nocred", "anthropic", priority=97)
    await store.delete("claude-nocred")
    await add(db, store, "codex-1", "openai", priority=10)
    assert (await lease(db, store, B)).account_id == full  # takes the only slot
    out = await exhaust_lease(db, store, "claude-out", clock)
    assert out is not None
    got = await lease(db, store, A)
    assert got.name == "codex-1"  # off, full, exhausted and credential-less skipped
    assert (await get(db, nocred, clock)).state is AccountState.DISABLED


async def exhaust_lease(db: Database, store, name: str, clock: FakeClock):
    """Exhaust the named account through a lease of a helper agent."""
    await insert_agent(db, "agent-x")
    async with db.write() as tx:
        async with tx.conn.execute(
            "SELECT id FROM accounts WHERE name = ?", (name,)
        ) as cur:
            account_id = (await cur.fetchone())["id"]
        await tx.conn.execute(
            "INSERT INTO account_leases (id, account_id, agent_id, providers,"
            " leased_at, role) VALUES ('l-x', ?, 'agent-x', '[]', ?, 'engine')",
            (account_id, ts(clock.now)),
        )
    return await exhaust(
        db, store, account_id, agent="agent-x", until=clock.now + timedelta(hours=5)
    )


async def test_gitea_and_executor_only_accounts_never_become_model_accounts(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "repo", "gitea", priority=1000)
    await add(db, store, "cursor-1", "cursor", priority=500)
    await add(db, store, "codex-1", "openai", priority=1)
    assert (await lease(db, store, A)).name == "codex-1"  # not gitea, not an engine
    async with db.write() as tx:
        executor = await accounts.lease(tx, A, None, store, "executor")
        repo = await accounts.lease(tx, A, None, store, "repository")
    assert executor.name == "cursor-1"
    assert repo.name == "repo"


async def test_runtime_providers_state_capability_not_preference(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "exotic", "mistral", priority=100)
    await add(db, store, "claude-1", "anthropic", priority=50)
    assert (await lease(db, store, A, ["anthropic", "openai"])).name == "claude-1"
    assert (await lease(db, store, B)).name == "exotic"  # no statement: global


async def test_max_leases_and_least_used(
    db: Database, store: MemorySecretStore
) -> None:
    await add(db, store, "solo", "anthropic", priority=1, max_leases=1)
    await add(db, store, "shared", "anthropic")
    assert (await lease(db, store, A)).name == "solo"
    assert (await lease(db, store, B)).name == "shared"  # solo is full


async def test_one_lease_per_agent(
    db: Database, store: MemorySecretStore, clock
) -> None:
    first = await add(db, store, "solo", "anthropic", max_leases=1)
    await lease(db, store, A)
    await lease(db, store, A)  # replacing its own lease frees the slot
    assert (await get(db, first, clock)).active_leases == 1


async def test_no_account(db: Database, store: MemorySecretStore) -> None:
    with pytest.raises(NoAccountError):
        await lease(db, store)


# -- exhaustion and fallback ---------------------------------------------------------------


async def test_fallback_ranks_remaining_accounts_globally(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic", priority=9)
    await add(db, store, "openai-1", "openai", priority=5)
    await add(db, store, "claude-2", "anthropic", priority=1)
    assert (await lease(db, store)).account_id == c1
    result = await exhaust(db, store, c1, until=clock.now + timedelta(hours=5))
    assert result.lease is not None
    assert result.lease.name == "openai-1"  # no preference for the previous provider
    exhausted = await get(db, c1, clock)
    assert exhausted.state is AccountState.EXHAUSTED
    assert exhausted.exhausted_until == clock.now + timedelta(hours=5)


async def test_recovery_is_considered_at_the_next_lease_without_preemption(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic", priority=100)
    o1 = await add(db, store, "openai-1", "openai", priority=10)
    assert (await lease(db, store, A)).account_id == c1
    fallback = await exhaust(db, store, c1, until=clock.now + timedelta(hours=1))
    assert fallback.lease is not None and fallback.lease.account_id == o1
    clock.advance(7200)
    async with db.write() as tx:
        await accounts.recover(tx)
    assert (await active(db, A)) == [("engine", o1)]  # active work is not preempted
    assert (await lease(db, store, B)).account_id == c1  # new work sees the recovery
    assert (await lease(db, store, A)).account_id == c1  # A's next lease, too


async def test_exhausted_report_with_nothing_left_blocks_across_providers(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic", priority=100)
    await lease(db, store, A)
    result = await exhaust(db, store, c1, until=clock.now + timedelta(hours=3))
    assert result.lease is None
    assert result.earliest_reset == clock.now + timedelta(hours=3)


async def test_default_cooldown_without_reset_time(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic")
    await lease(db, store)
    await exhaust(db, store, c1)
    assert (await get(db, c1, clock)).exhausted_until == clock.now + timedelta(hours=1)


async def test_nothing_left_blocks_task_until_an_account_recovers(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic")
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
    assert task is not None
    await lease(db, store)
    reset = clock.now + timedelta(minutes=30)
    result = await exhaust(
        db, store, c1, until=reset, task_id=task.id, attempt=task.attempt
    )
    assert (result.lease, result.earliest_reset, result.task_blocked) == (
        None,
        reset,
        True,
    )
    async with db.read() as conn:
        blocked = await tasks.get_task(conn, task.id)
    assert (blocked.state, blocked.block_reason) == (S.BLOCKED, "awaiting_quota")

    clock.advance(30 * 60)
    async with db.write() as tx:
        result = await reap(tx)
    assert result.recovered_accounts == [c1]
    async with db.read() as conn:
        assert (await tasks.get_task(conn, task.id)).state is S.READY


async def test_exhausted_requires_holding_the_lease(
    db: Database, store: MemorySecretStore
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic")
    with pytest.raises(ConflictError) as err:
        await exhaust(db, store, c1)
    assert err.value.code == "not_leased"


async def test_lost_agent_releases_its_lease(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "solo", "anthropic", max_leases=1)
    await lease(db, store, A)
    with pytest.raises(NoAccountError):  # the only slot is A's
        await lease(db, store, B)
    clock.advance(91)
    async with db.write() as tx:
        await tx.conn.execute(  # B stays alive; A goes silent
            "UPDATE agents SET last_heartbeat_at = ? WHERE id = ?", (ts(clock.now), B)
        )
        await reap(tx)
    assert (await get(db, c1, clock)).active_leases == 0
    assert (await lease(db, store, B)).account_id == c1


# -- secrets reconciliation and admin controls ------------------------------------------------


async def test_missing_secret_disables_and_its_return_reenables(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic")
    await store.delete("claude-1")
    async with db.write() as tx:
        await accounts.reconcile_secrets(tx, store)
    gone = await get(db, c1, clock)
    assert gone.state is AccountState.DISABLED
    assert gone.disabled_reason == "secret not found: sops:claude-1"
    await store.put("claude-1", {"api_key": "new"})
    async with db.write() as tx:
        await accounts.reconcile_secrets(tx, store)
    assert (await get(db, c1, clock)).state is AccountState.AVAILABLE


async def test_admin_disable_is_not_undone_by_reconcile(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic")
    async with db.write() as tx:
        await accounts.update_account(
            tx, c1, AccountUpdate(enabled=False), "admin:test"
        )
        await accounts.reconcile_secrets(tx, store)
    assert (await get(db, c1, clock)).state is AccountState.DISABLED
    with pytest.raises(NoAccountError):
        await lease(db, store)


# -- usage and credentials ----------------------------------------------------------------------


async def test_usage_from_results_and_reports(
    db: Database, store: MemorySecretStore, clock: FakeClock
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic")
    async with db.write() as tx:
        with pytest.raises(ForbiddenError):  # only the lease holder reports
            await accounts.record_usage(tx, c1, A, UsageReport(input_tokens=1))
    await lease(db, store, A)
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
        assert task is not None
        await accounts.record_usage(
            tx,
            c1,
            A,
            UsageReport(input_tokens=100, cached_input_tokens=40, cost_usd=0.5),
        )
        await tasks.complete(
            tx,
            A,
            task.id,
            task.attempt,
            TaskResult(
                summary="done",
                usage=Usage(
                    account_id=c1, input_tokens=20, output_tokens=7, cost_usd=0.25
                ),
            ),
        )
    account = await get(db, c1, clock)
    assert (account.usage_total.input_tokens, account.usage_total.output_tokens) == (
        120,
        7,
    )
    assert account.usage_total.cost_usd == pytest.approx(0.75)
    assert (account.usage_total.cached_input_tokens, account.usage_total.runs) == (
        40,
        2,
    )
    clock.advance(timedelta(days=2).total_seconds())
    assert (await get(db, c1, clock)).usage_24h.input_tokens == 0


async def test_only_the_lease_holder_may_replace_a_credential(
    db: Database, store: MemorySecretStore
) -> None:
    c1 = await add(db, store, "claude-1", "anthropic")
    await lease(db, store, A)
    async with db.read() as conn:
        assert await accounts.secret_name_for_update(conn, c1, A) == "claude-1"
        assert (
            await accounts.secret_name_for_update(conn, c1, None) == "claude-1"
        )  # admin
        with pytest.raises(ForbiddenError):
            await accounts.secret_name_for_update(conn, c1, B)


# -- HTTP -----------------------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "a" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class FailingStore(MemorySecretStore):
    async def put(self, name: str, value: dict) -> None:
        if name.startswith("account-") and "refresh" in value:
            raise SecretStoreError("disk full")
        await super().put(name, value)


async def lease_role(db, store, role, runtime=None, agent=A):
    async with db.write() as tx:
        return await accounts.lease(tx, agent, runtime, store, role)


async def active(db: Database, agent=A) -> list[tuple[str, str]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT role, account_id FROM account_leases"
            " WHERE agent_id = ? AND released_at IS NULL ORDER BY role",
            (agent,),
        ) as cur,
    ):
        return [(r["role"], r["account_id"]) for r in await cur.fetchall()]


async def test_engine_and_executor_leases_coexist(db: Database, store) -> None:
    codex = await add(db, store, "codex-1", "openai", priority=10)
    claude = await add(db, store, "claude-1", "anthropic")
    engine = await lease_role(db, store, "engine", ["openai", "anthropic"])
    executor = await lease_role(db, store, "executor", ["anthropic"])
    assert (engine.account_id, engine.role) == (codex, "engine")
    assert (executor.account_id, executor.role) == (claude, "executor")
    assert await active(db) == [("engine", codex), ("executor", claude)]
    # Re-leasing a role replaces only that role.
    await lease_role(db, store, "engine", ["openai"])
    assert await active(db) == [("engine", codex), ("executor", claude)]
    async with db.write() as tx:
        await accounts.release(tx, A, "released", "executor")
    assert await active(db) == [("engine", codex)]
    async with db.write() as tx:
        await accounts.release(tx, A, "released")
    assert await active(db) == []


async def test_a_lower_priority_executor_never_displaces_the_held_engine_account(
    db: Database, store
) -> None:
    claude = await add(db, store, "claude-1", "anthropic", priority=100, max_leases=1)
    codex = await add(db, store, "codex-1", "openai", priority=0)
    engine = await lease_role(db, store, "engine")
    assert engine.account_id == claude
    # Plain executor lease: the engine's slot is taken, so the next best is chosen.
    async with db.write() as tx:
        plain = await accounts.lease(tx, A, None, store, "executor")
    assert plain.account_id == codex
    # With reuse_held the held, higher-priority account stays the executor's pick:
    # its own lease is returned, no second row, no extra slot, no Codex lease.
    async with db.write() as tx:
        reused = await accounts.lease(tx, A, None, store, "executor", reuse_held=True)
    assert (reused.account_id, reused.role) == (claude, "engine")
    assert reused.lease_id == engine.lease_id
    assert reused.credential == {"api_key": "key-claude-1"}
    assert await active(db) == [("engine", claude)]  # the codex executor was replaced
    # Another agent still finds Claude full (capacity is not inflated).
    assert (await lease(db, store, B)).account_id == codex


async def test_reuse_held_prefers_a_better_other_account_and_other_roles_ignore_it(
    db: Database, store
) -> None:
    held = await add(db, store, "codex-1", "openai", priority=10)
    better = await add(db, store, "claude-1", "anthropic", priority=100)
    async with db.write() as tx:  # force the engine onto the lower account
        await accounts.lease(tx, A, ["openai"], store)
    async with db.write() as tx:
        executor = await accounts.lease(tx, A, None, store, "executor", reuse_held=True)
    assert (executor.account_id, executor.role) == (better, "executor")
    assert ("engine", held) in await active(db)
    # reuse_held means nothing for the engine role.
    async with db.write() as tx:
        again = await accounts.lease(tx, A, None, store, "engine", reuse_held=True)
    assert again.role == "engine"


async def test_executor_exhaustion_stays_in_its_role(db: Database, store) -> None:
    await add(db, store, "codex-1", "openai", priority=5)
    c1 = await add(db, store, "claude-1", "anthropic", priority=2)
    c2 = await add(db, store, "claude-2", "anthropic", priority=1)
    engine = await lease_role(db, store, "engine", ["openai", "anthropic"])
    executor = await lease_role(db, store, "executor", ["anthropic"])
    assert executor.account_id == c1
    async with db.write() as tx:
        result = await accounts.exhausted(
            tx,
            A,
            c1,
            ExhaustedReport(
                reason="limit", role="executor", runtime_providers=["anthropic"]
            ),
            store,
        )
    # Ranked globally among what the runtime can apply; the role is kept.
    assert result.lease and (result.lease.account_id, result.lease.role) == (
        c2,
        "executor",
    )
    assert ("engine", engine.account_id) in await active(db)
    assert ("executor", c2) in await active(db)


async def test_repository_lease_is_independent_and_released_without_exposing_secret(
    db: Database, store
) -> None:
    engine_id = await add(db, store, "engine-account", "anthropic")
    repo_id = await add(db, store, "repo-account", "gitea")
    await lease_role(db, store, "engine", ["anthropic"])
    repository = await lease_role(db, store, "repository", ["gitea"])
    assert await active(db) == [("engine", engine_id), ("repository", repo_id)]
    assert repository.role == "repository"
    assert (
        "key-repo-account"
        not in (await get(db, repo_id, FakeClock())).model_dump_json()
    )
    async with db.write() as tx:
        await accounts.release(tx, A, "released", "repository")
    assert await active(db) == [("engine", engine_id)]


async def test_agents_lease_independently_and_credentials_follow_the_lease(
    db: Database, store
) -> None:
    claude = await add(db, store, "claude-1", "anthropic")
    await lease_role(db, store, "executor", ["anthropic"], agent=A)
    await lease_role(db, store, "executor", ["anthropic"], agent=B)
    assert await active(db, A) == [("executor", claude)]
    assert await active(db, B) == [("executor", claude)]
    async with db.read() as conn:
        assert await accounts.secret_name_for_update(conn, claude, A) == "claude-1"
        with pytest.raises(ForbiddenError):
            await accounts.secret_name_for_update(conn, claude, "agent-x")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = FailingStore()
        yield c


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


def test_accounts_over_http(client: TestClient) -> None:
    res = client.post(
        "/api/accounts",
        json={
            "name": "claude-1",
            "provider": "anthropic",
            "credential": {"api_key": "sk-ant-api03-fake"},
        },
        headers=ADMIN,
    )
    assert res.status_code == 201
    account = res.json()
    assert account["secret_ref"] == f"sops:account-{account['id']}"
    assert "sk-ant-api03-fake" not in res.text
    assert "sk-ant-api03-fake" not in client.get("/api/accounts", headers=ADMIN).text

    worker = register(client)
    assert client.get("/api/accounts", headers=worker).status_code == 403
    leased = client.post(
        "/api/accounts/lease", json={"providers": ["anthropic"]}, headers=worker
    ).json()
    assert leased["credential"] == {"api_key": "sk-ant-api03-fake"}

    # A credential that cannot be persisted disables the account loudly.
    res = client.put(
        f"/api/accounts/{account['id']}/credential",
        json={"credential": {"api_key": "sk-ant-api03-y", "refresh": "r"}},
        headers=worker,
    )
    assert res.status_code == 503
    after = client.get(f"/api/accounts/{account['id']}", headers=ADMIN).json()
    assert after["state"] == "disabled"
    assert after["disabled_reason"].startswith("credential write failed")


def test_create_needs_exactly_one_secret_source(client: TestClient) -> None:
    for body in (
        {"name": "x", "provider": "anthropic"},
        {
            "name": "x",
            "provider": "anthropic",
            "credential": {},
            "secret_ref": "sops:x",
        },
    ):
        assert client.post("/api/accounts", json=body, headers=ADMIN).status_code == 422


def test_gitea_service_credential_is_sops_backed_and_leased_only_to_repository_role(
    client: TestClient,
) -> None:
    token = "dummy-gitea-service-token"
    created = client.post(
        "/api/accounts",
        json={
            "name": "repository-writer",
            "provider": "gitea",
            "credential": {"username": "argus-writer", "token": token},
        },
        headers=ADMIN,
    )
    assert created.status_code == 201
    assert created.json()["secret_ref"].startswith("sops:account-")
    assert token not in created.text
    assert token not in client.get("/api/accounts", headers=ADMIN).text
    worker = register(client)
    leased = client.post(
        "/api/accounts/lease",
        json={"providers": ["gitea"], "role": "repository"},
        headers=worker,
    )
    assert leased.status_code == 200
    assert leased.json()["role"] == "repository"
    assert leased.json()["credential"] == {"username": "argus-writer", "token": token}


@pytest.mark.parametrize(
    "credential", [{"token": "dummy"}, {"username": "bad:user", "token": "dummy"}]
)
def test_gitea_account_requires_valid_basic_auth_username(
    client: TestClient, credential
) -> None:
    response = client.post(
        "/api/accounts",
        json={"name": "repo-writer", "provider": "gitea", "credential": credential},
        headers=ADMIN,
    )
    assert response.status_code == 422
    assert "dummy" not in response.text


async def test_one_account_held_in_both_roles_exhausts_per_role(
    db: Database, store
) -> None:
    only = await add(db, store, "claude-1", "anthropic")
    await lease_role(db, store, "engine", ["anthropic"])
    await lease_role(db, store, "executor", ["anthropic"])
    async with db.write() as tx:
        result = await accounts.exhausted(
            tx, A, only, ExhaustedReport(reason="limit", role="executor"), store
        )
    assert result.lease is None
    assert await active(db) == [("engine", only)]


def _create(client: TestClient, name: str) -> dict:
    res = client.post(
        "/api/accounts",
        json={"name": name, "provider": "acme", "credential": {"api_key": "k"}},
        headers=ADMIN,
    )
    assert res.status_code == 201
    return res.json()


def test_rest_lease_is_global_and_the_retired_provider_list_restricts_nothing(
    client: TestClient,
) -> None:
    for name, provider, priority in (
        ("big", "acme", 100),
        ("small", "zeta", 1),
    ):
        res = client.post(
            "/api/accounts",
            json={
                "name": name,
                "provider": provider,
                "priority": priority,
                "credential": {"api_key": f"key-{name}"},
            },
            headers=ADMIN,
        )
        assert res.status_code == 201
    worker = register(client)
    # The retired `providers` list is accepted and ignored.
    legacy = client.post(
        "/api/accounts/lease", json={"providers": ["zeta"]}, headers=worker
    )
    assert legacy.status_code == 200 and legacy.json()["name"] == "big"
    # `runtime_providers` is capability: the best account the runtime can apply.
    capable = client.post(
        "/api/accounts/lease", json={"runtime_providers": ["zeta"]}, headers=worker
    )
    assert capable.status_code == 200 and capable.json()["name"] == "small"
    # The credential rides only in the lease response, never in a listing.
    listing = client.get("/api/accounts", headers=ADMIN)
    assert "key-big" not in listing.text and "key-small" not in listing.text
    # No account is usable for a runtime that supports none of them.
    none = client.post(
        "/api/accounts/lease", json={"runtime_providers": ["nope"]}, headers=worker
    )
    assert none.status_code == 409 and none.json()["detail"]["code"] == "no_account"


def test_edit_fields_and_name_conflict(client: TestClient) -> None:
    one, two = _create(client, "one"), _create(client, "two")
    res = client.patch(
        f"/api/accounts/{one['id']}",
        json={"name": "renamed", "priority": 5, "max_leases": 2, "enabled": False},
        headers=ADMIN,
    )
    body = res.json()
    assert (body["name"], body["priority"], body["max_leases"]) == ("renamed", 5, 2)
    assert body["state"] == "disabled"
    assert body["secret_ref"] == one["secret_ref"]
    res = client.patch(
        f"/api/accounts/{two['id']}", json={"name": "renamed"}, headers=ADMIN
    )
    assert (res.status_code, res.json()["detail"]["code"]) == (409, "name_taken")
    # Renaming to its own name is a no-op, not a conflict.
    res = client.patch(
        f"/api/accounts/{two['id']}", json={"name": "two"}, headers=ADMIN
    )
    assert res.status_code == 200


def test_delete_needs_admin_and_the_current_name(client: TestClient) -> None:
    account = _create(client, "doomed")
    url = f"/api/accounts/{account['id']}"
    assert client.delete(url, params={"confirm_name": "doomed"}).status_code == 401
    assert client.delete(url, headers=ADMIN).status_code == 422
    res = client.delete(url, params={"confirm_name": "other"}, headers=ADMIN)
    assert (res.status_code, res.json()["detail"]["code"]) == (409, "name_mismatch")
    client.patch(url, json={"name": "renamed"}, headers=ADMIN)
    res = client.delete(url, params={"confirm_name": "doomed"}, headers=ADMIN)
    assert (res.status_code, res.json()["detail"]["code"]) == (409, "name_mismatch")
    assert client.get(url, headers=ADMIN).status_code == 200
    res = client.delete(url, params={"confirm_name": "renamed"}, headers=ADMIN)
    assert res.status_code == 204
    assert client.get(url, headers=ADMIN).status_code == 404
    assert (
        client.delete(
            url, params={"confirm_name": "renamed"}, headers=ADMIN
        ).status_code
        == 404
    )


def test_delete_refuses_active_lease_then_succeeds_once_released(
    client: TestClient,
) -> None:
    account = _create(client, "busy")
    url = f"/api/accounts/{account['id']}"
    worker = register(client)
    client.post("/api/accounts/lease", json={"providers": ["acme"]}, headers=worker)
    res = client.delete(url, params={"confirm_name": "busy"}, headers=ADMIN)
    assert (res.status_code, res.json()["detail"]["code"]) == (409, "account_in_use")
    assert client.get(url, headers=ADMIN).json()["active_leases"] == 1
    client.post("/api/accounts/release", headers=worker)
    assert (
        client.delete(url, params={"confirm_name": "busy"}, headers=ADMIN).status_code
        == 204
    )


def test_delete_removes_usage_leases_and_secret_but_not_other_accounts(
    client: TestClient,
) -> None:
    gone, kept = _create(client, "gone"), _create(client, "kept")
    worker = register(client)
    leased = client.post(
        "/api/accounts/lease", json={"providers": ["acme"]}, headers=worker
    ).json()
    client.post(
        f"/api/accounts/{leased['account_id']}/usage",
        json={"input_tokens": 3, "output_tokens": 4, "cost_usd": 0.1},
        headers=worker,
    )
    client.post("/api/accounts/release", headers=worker)
    # Disabled accounts, with history, are deletable.
    for account in (gone, kept):
        client.patch(
            f"/api/accounts/{account['id']}", json={"enabled": False}, headers=ADMIN
        )
    secrets = app.state.secrets
    assert f"account-{gone['id']}" in secrets.names()
    res = client.delete(
        f"/api/accounts/{gone['id']}", params={"confirm_name": "gone"}, headers=ADMIN
    )
    assert res.status_code == 204
    assert f"account-{gone['id']}" not in secrets.names()
    assert f"account-{kept['id']}" in secrets.names()
    names = [a["name"] for a in client.get("/api/accounts", headers=ADMIN).json()]
    assert names == ["kept"]


def test_delete_rolls_back_when_the_secret_store_fails(client: TestClient) -> None:
    account = _create(client, "sticky")
    url = f"/api/accounts/{account['id']}"
    secrets = app.state.secrets

    async def fail(name: str) -> None:
        raise SecretStoreError("sops unset failed")

    original, secrets.delete = secrets.delete, fail
    try:
        res = client.delete(url, params={"confirm_name": "sticky"}, headers=ADMIN)
    finally:
        secrets.delete = original
    assert res.status_code == 503
    assert client.get(url, headers=ADMIN).status_code == 200
    assert f"account-{account['id']}" in secrets.names()


def test_delete_leaves_an_operator_managed_secret(
    client: TestClient,
) -> None:
    secrets = app.state.secrets
    asyncio.run(secrets.put("shared", {"api_key": "k"}))
    res = client.post(
        "/api/accounts",
        json={"name": "ref", "provider": "acme", "secret_ref": "sops:shared"},
        headers=ADMIN,
    )
    res = client.delete(
        f"/api/accounts/{res.json()['id']}",
        params={"confirm_name": "ref"},
        headers=ADMIN,
    )
    assert res.status_code == 204
    assert "shared" in secrets.names()


def test_delete_commit_failure_after_secret_removal_restores_the_exact_secret(
    client: TestClient,
) -> None:
    account = _create(client, "fragile")
    url = f"/api/accounts/{account['id']}"
    secrets = app.state.secrets
    name = f"account-{account['id']}"
    prior = dict(secrets.get(name))
    writer = app.state.db._writer
    original_execute, original_delete = writer.execute, secrets.delete
    removed = False

    async def delete(secret: str) -> None:
        nonlocal removed
        await original_delete(secret)
        removed = True

    async def boom() -> None:
        raise OSError("disk I/O error")

    def execute(sql, *args, **kwargs):
        if sql == "COMMIT" and removed:
            return boom()
        return original_execute(sql, *args, **kwargs)

    writer.execute, secrets.delete = execute, delete
    try:
        with pytest.raises(OSError, match="disk I/O"):
            client.delete(url, params={"confirm_name": "fragile"}, headers=ADMIN)
    finally:
        writer.execute, secrets.delete = original_execute, original_delete
    assert removed
    assert secrets.get(name) == prior
    after = client.get(url, headers=ADMIN)
    assert after.status_code == 200 and after.json()["name"] == "fragile"
    # The writer is usable again and the delete can be retried.
    res = client.delete(url, params={"confirm_name": "fragile"}, headers=ADMIN)
    assert res.status_code == 204 and secrets.get(name) is None


def test_delete_keeps_a_secret_another_account_still_references(
    client: TestClient,
) -> None:
    owner = _create(client, "owner")
    secrets = app.state.secrets
    name = f"account-{owner['id']}"
    sibling = client.post(
        "/api/accounts",
        json={"name": "sibling", "provider": "acme", "secret_ref": f"sops:{name}"},
        headers=ADMIN,
    ).json()
    res = client.delete(
        f"/api/accounts/{owner['id']}", params={"confirm_name": "owner"}, headers=ADMIN
    )
    assert res.status_code == 204 and secrets.get(name) is not None
    worker = register(client)
    leased = client.post(
        "/api/accounts/lease", json={"providers": ["acme"]}, headers=worker
    ).json()
    assert leased["account_id"] == sibling["id"] and leased["credential"]
    client.post("/api/accounts/release", headers=worker)
    # The last referrer takes the generated secret with it.
    res = client.delete(
        f"/api/accounts/{sibling['id']}",
        params={"confirm_name": "sibling"},
        headers=ADMIN,
    )
    assert res.status_code == 204 and secrets.get(name) is None


def test_replace_credential_keeps_state_and_validates_shape(
    client: TestClient,
) -> None:
    account = _create(client, "cursor-ish")
    url = f"/api/accounts/{account['id']}"
    client.patch(url, json={"enabled": False, "priority": 4}, headers=ADMIN)
    cred = f"{url}/credential"
    res = client.put(cred, json={"credential": {}}, headers=ADMIN)
    assert res.status_code == 422 and "non-empty" in res.text
    res = client.put(
        cred, json={"credential": {"accessToken": "new-secret-xyz"}}, headers=ADMIN
    )
    assert res.status_code == 204 and "new-secret-xyz" not in res.text
    assert app.state.secrets.get(f"account-{account['id']}") == {
        "accessToken": "new-secret-xyz"
    }
    after = client.get(url, headers=ADMIN).json()
    assert (after["state"], after["priority"], after["name"]) == (
        "disabled",
        4,
        "cursor-ish",
    )
    assert "new-secret-xyz" not in client.get("/api/accounts", headers=ADMIN).text
