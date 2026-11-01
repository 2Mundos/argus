"""Copilot accounts: token validation over REST, admin-only replacement, the
executor-only lease and the quota reason (copilot accounts spec). Fake tokens only."""

from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from api_support import FakeClock, insert_agent
from argus_api.accounts import service as accounts
from argus_api.accounts.service import NoAccountError
from argus_api.db import Database, ts
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, AccountUpdate
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

ADMIN_TOKEN = "t" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
BODY = "FAKECOPILOTBODY0123456789"
TOKEN = "github_pat_" + BODY
OTHER = "gho_" + "REPLACEMENTFAKE0123456789"
CLASSIC = "ghp_" + BODY
SECRETS = (BODY, "REPLACEMENTFAKE0123456789", "UNKNOWNFAKE0123456789")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def create(client: TestClient, name: str, credential: dict, **extra):
    return client.post(
        "/api/accounts",
        json={"name": name, "provider": "copilot", "credential": credential, **extra},
        headers=ADMIN,
    )


def secret_of(account: dict) -> dict:
    return app.state.secrets.get(account["secret_ref"].removeprefix("sops:"))


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


def test_create_derives_the_subscription_kind_and_never_echoes_the_token(
    client: TestClient,
) -> None:
    for prefix in ("github_pat_", "gho_", "ghu_"):
        res = create(client, prefix, {"token": prefix + BODY}, kind="api_key")
        assert res.status_code == 201, res.text
        assert res.json()["kind"] == "subscription"
        assert res.json()["state"] == "available"
        assert BODY not in res.text
        assert BODY not in client.get("/api/accounts", headers=ADMIN).text


@pytest.mark.parametrize(
    ("credential", "needle"),
    [
        ({"token": CLASSIC}, "classic personal access token"),
        ({"token": "sk-UNKNOWNFAKE0123456789"}, "token must start with one of"),
        ({"token": ""}, "non-empty string"),
        ({"token": "gho_ " + BODY}, "must not contain whitespace"),
        ({"token": TOKEN, "extra": "x"}, "remove extra"),
        ({"token": 7}, "non-empty string"),
        ({}, "non-empty string"),
    ],
)
def test_create_rejects_with_field_and_rule_only(
    client: TestClient, credential, needle: str
) -> None:
    res = create(client, "bad", credential)
    assert res.status_code == 422 and needle in res.text
    for secret in SECRETS:
        assert secret not in res.text
    assert client.get("/api/accounts", headers=ADMIN).json() == []  # nothing stored


def test_a_secret_ref_is_not_a_copilot_credential(client: TestClient) -> None:
    res = client.post(
        "/api/accounts",
        json={"name": "ref", "provider": "copilot", "secret_ref": "sops:existing"},
        headers=ADMIN,
    )
    assert res.status_code == 422
    assert client.get("/api/accounts", headers=ADMIN).json() == []


def test_admin_replaces_the_token_and_the_listing_never_shows_it(
    client: TestClient,
) -> None:
    created = create(client, "main", {"token": TOKEN}, priority=4).json()
    res = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={"credential": {"token": OTHER}},
        headers=ADMIN,
    )
    assert res.status_code == 204
    assert secret_of(created) == {"token": OTHER}
    after = client.get(f"/api/accounts/{created['id']}", headers=ADMIN)
    assert (after.json()["kind"], after.json()["priority"]) == ("subscription", 4)
    for secret in SECRETS:
        assert secret not in after.text
        assert secret not in client.get("/api/accounts", headers=ADMIN).text


@pytest.mark.parametrize(
    "credential", [{"token": CLASSIC}, {"token": OTHER, "extra": "x"}]
)
def test_an_invalid_replacement_leaves_the_stored_secret_unchanged(
    client: TestClient, credential
) -> None:
    created = create(client, "main", {"token": TOKEN}).json()
    res = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={"credential": credential},
        headers=ADMIN,
    )
    assert res.status_code == 422
    for secret in SECRETS:
        assert secret not in res.text
    assert secret_of(created) == {"token": TOKEN}


def test_a_leasing_agent_cannot_replace_a_copilot_credential(
    client: TestClient,
) -> None:
    created = create(client, "main", {"token": TOKEN}).json()
    worker = register(client)
    task = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "copilot test"},
        headers=ADMIN,
    )
    assert task.status_code == 201
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    leased = client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["copilot"],
            "role": "executor",
            "task_id": claimed["id"],
            "attempt": claimed["attempt"],
        },
        headers=worker,
    )
    assert leased.status_code == 200, leased.text
    res = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={
            "credential": {"token": OTHER},
            "task_id": claimed["id"],
            "attempt": claimed["attempt"],
        },
        headers=worker,
    )
    assert res.status_code == 403 and OTHER not in res.text
    assert secret_of(created) == {"token": TOKEN}


# -- role selection and quota (service level) -----------------------------------------

A = "agent-a"
CREDENTIAL = {"token": TOKEN}


@pytest.fixture
async def seeded(db: Database) -> MemorySecretStore:
    await insert_agent(db, A)
    return MemorySecretStore()


async def add(db: Database, store, name: str, provider: str, credential: dict, **kw):
    await store.put(name, credential)
    async with db.write() as tx:
        return (
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
        ).id


async def lease(db: Database, store, role: str, providers: list[str]):
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="demo", title="copilot lease test"), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
    assert task is not None
    async with db.write() as tx:
        return await accounts.lease(
            tx, A, providers, store, role, task_id=task.id, attempt=task.attempt
        )


async def test_the_engine_role_never_leases_copilot_even_at_top_priority(
    db: Database, seeded: MemorySecretStore
) -> None:
    await add(db, seeded, "copilot-1", "copilot", CREDENTIAL, priority=1000)
    codex = await add(db, seeded, "codex-1", "openai", {"OPENAI_API_KEY": "k"})
    engine = await lease(db, seeded, "engine", ["copilot", "openai"])
    assert (engine.account_id, engine.provider) == (codex, "openai")


async def test_the_engine_role_alone_finds_no_copilot_account(
    db: Database, seeded: MemorySecretStore
) -> None:
    await add(db, seeded, "copilot-1", "copilot", CREDENTIAL)
    with pytest.raises(NoAccountError):
        await lease(db, seeded, "engine", ["copilot"])


async def test_the_executor_role_leases_copilot_when_it_ranks_first(
    db: Database, seeded: MemorySecretStore
) -> None:
    copilot = await add(db, seeded, "copilot-1", "copilot", CREDENTIAL, priority=10)
    await add(db, seeded, "codex-1", "openai", {"OPENAI_API_KEY": "k"}, priority=1)
    executor = await lease(db, seeded, "executor", ["copilot", "openai"])
    assert (executor.account_id, executor.role, executor.provider) == (
        copilot,
        "executor",
        "copilot",
    )
    assert executor.credential == CREDENTIAL


async def test_a_disabled_or_exhausted_copilot_account_is_skipped(
    db: Database, seeded: MemorySecretStore, clock: FakeClock
) -> None:
    disabled = await add(db, seeded, "copilot-off", "copilot", CREDENTIAL, priority=9)
    exhausted = await add(db, seeded, "copilot-out", "copilot", CREDENTIAL, priority=8)
    ready = await add(db, seeded, "copilot-on", "copilot", CREDENTIAL, priority=1)
    async with db.write() as tx:
        await accounts.update_account(tx, disabled, AccountUpdate(enabled=False), "op")
        await tx.conn.execute(
            "UPDATE accounts SET state = 'exhausted', exhausted_until = ? WHERE id = ?",
            (ts(clock.now + timedelta(hours=5)), exhausted),
        )
    executor = await lease(db, seeded, "executor", ["copilot"])
    assert executor.account_id == ready


async def test_a_copilot_account_reads_unknown_with_no_provider_source(
    db: Database, seeded: MemorySecretStore, clock: FakeClock
) -> None:
    copilot = await add(db, seeded, "copilot-1", "copilot", CREDENTIAL)
    async with db.read() as conn:
        account = await accounts.get_account(conn, copilot, clock.now)
    status = account.quota_status
    assert (status.state, status.reason) == ("unknown", "no_provider_source")
    assert (status.windows, status.observed_at, status.last_probe) == ([], None, None)
    assert account.quota == []
