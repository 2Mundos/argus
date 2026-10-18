"""Cursor accounts over REST: two credential shapes, expiry, renewal, and the
executor lease (cursor accounts spec). Fake, unsigned JWT-shaped tokens only."""

import base64
import json
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.accounts.service import NoAccountError
from argus_api.db import Database
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_core.accounts import AccountCreate, AccountUpdate
from fastapi.testclient import TestClient

ADMIN_TOKEN = "t" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def jwt(exp: float | None, tag: str = "A") -> str:
    def b(o) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    claims = {"sub": f"auth0|{tag}", **({"exp": int(exp)} if exp else {})}
    return f"{b({'alg': 'none'})}.{b(claims)}.fake{tag}sig"


FUTURE = time.time() + 30 * 86400
ACCESS = jwt(FUTURE, "ACCESSFAKE")
REFRESH = jwt(FUTURE, "REFRESHFAKE")
EXPIRED = jwt(time.time() - 3600, "EXPIREDFAKE")
API_KEY = "key_" + "FAKEAPIKEY" * 4
LOGIN = {"accessToken": ACCESS, "refreshToken": REFRESH}
SETTINGS_ONLY = {
    "version": 1,
    "permissions": {"allow": [], "deny": []},
    "editor": {"vimMode": False},
    "model": {"modelId": "composer-2.5"},
    "authInfo": {"authId": "auth0|SETTINGSFAKE", "email": "someone@example.test"},
}
SECRETS = (ACCESS, REFRESH, EXPIRED, API_KEY, "SETTINGSFAKE", "someone@example.test")


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
        json={"name": name, "provider": "cursor", "credential": credential, **extra},
        headers=ADMIN,
    )


def test_both_shapes_are_accepted_and_the_kind_is_derived(client: TestClient) -> None:
    for name, credential, kind in [
        ("key", {"api_key": API_KEY}, "api_key"),
        ("login", LOGIN, "subscription"),
    ]:
        res = create(client, name, credential, kind="api_key")  # the form's guess loses
        assert res.status_code == 201, res.text
        assert res.json()["kind"] == kind
        assert res.json()["state"] == "available"
        for secret in SECRETS:
            assert secret not in res.text
            assert secret not in client.get("/api/accounts", headers=ADMIN).text


@pytest.mark.parametrize(
    ("credential", "needle"),
    [
        (SETTINGS_ONLY, "a Cursor settings file is not a credential"),
        ({"accessToken": ACCESS}, "missing: refreshToken"),
        ({"accessToken": EXPIRED, "refreshToken": REFRESH}, "accessToken is expired"),
        ({"accessToken": "not-a-jwt", "refreshToken": REFRESH}, "not a JWT"),
        ({"api_key": ACCESS}, "put it in accessToken"),
        ({"api_key": API_KEY, "accessToken": ACCESS}, "only one of"),
    ],
)
def test_import_rejects_with_field_names_only(
    client: TestClient, credential, needle
) -> None:
    res = create(client, "bad", credential)
    assert res.status_code == 422 and needle in res.text
    for secret in SECRETS:
        assert secret not in res.text
    assert client.get("/api/accounts", headers=ADMIN).json() == []  # nothing stored


def test_renewing_keeps_settings_and_a_disabled_account_stays_disabled(
    client: TestClient,
) -> None:
    created = create(
        client,
        "main",
        {"api_key": API_KEY},
        priority=7,
        max_leases=3,
        labels={"team": "a"},
    ).json()
    client.patch(
        f"/api/accounts/{created['id']}", json={"enabled": False}, headers=ADMIN
    )
    res = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={"credential": LOGIN},
        headers=ADMIN,
    )
    assert res.status_code == 204
    after = client.get(f"/api/accounts/{created['id']}", headers=ADMIN).json()
    assert (after["name"], after["priority"], after["max_leases"], after["labels"]) == (
        "main",
        7,
        3,
        {"team": "a"},
    )
    assert after["kind"] == "subscription"  # follows the new credential
    assert after["state"] == "disabled"  # replacing a credential never enables
    ref = created["secret_ref"].removeprefix("sops:")
    assert app.state.secrets.get(ref) == LOGIN


@pytest.mark.parametrize(
    "credential",
    [SETTINGS_ONLY, {"accessToken": EXPIRED, "refreshToken": REFRESH}],
)
def test_a_bad_replacement_changes_nothing(client: TestClient, credential) -> None:
    created = create(client, "main", LOGIN).json()
    ref = created["secret_ref"].removeprefix("sops:")
    res = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={"credential": credential},
        headers=ADMIN,
    )
    assert res.status_code == 422
    for secret in SECRETS:
        assert secret not in res.text
    assert app.state.secrets.get(ref) == LOGIN


# -- the executor lease (service level) ----------------------------------------------

A = "agent-a"


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
        return await accounts.lease(tx, A, providers, store, role)


async def test_cursor_is_leased_as_the_executor_next_to_an_engine_account(
    db: Database, seeded: MemorySecretStore
) -> None:
    """The executor slot is a role of its own: a Cursor lease never replaces the
    engine's (codex/claude) and carries the stored document to the trusted host."""
    codex = await add(db, seeded, "codex-1", "openai", {"OPENAI_API_KEY": "k"})
    cursor = await add(db, seeded, "cursor-1", "cursor", LOGIN)
    engine = await lease(db, seeded, "engine", ["openai", "anthropic"])
    executor = await lease(db, seeded, "executor", ["cursor"])
    assert (engine.account_id, engine.role) == (codex, "engine")
    assert (executor.account_id, executor.role, executor.provider) == (
        cursor,
        "executor",
        "cursor",
    )
    assert executor.credential == LOGIN
    async with db.write() as tx:
        await accounts.release(tx, A, "released", "executor")
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT role FROM account_leases WHERE agent_id = ? AND released_at IS NULL",
            (A,),
        ) as cur,
    ):
        assert [r["role"] for r in await cur.fetchall()] == ["engine"]


async def test_a_disabled_cursor_account_is_never_leased_or_re_enabled_by_a_lease(
    db: Database, seeded: MemorySecretStore, clock
) -> None:
    cursor = await add(db, seeded, "cursor-1", "cursor", LOGIN)
    async with db.write() as tx:
        await accounts.update_account(tx, cursor, AccountUpdate(enabled=False), "op")
    with pytest.raises(NoAccountError):
        await lease(db, seeded, "executor", ["cursor"])
    await seeded.put("cursor-1", {"accessToken": jwt(FUTURE), "refreshToken": REFRESH})
    with pytest.raises(NoAccountError):  # a new credential does not enable it either
        await lease(db, seeded, "executor", ["cursor"])
    async with db.read() as conn:
        assert (await accounts.get_account(conn, cursor, clock.now)).state == "disabled"


async def test_an_account_whose_secret_is_gone_is_not_leased(
    db: Database, seeded: MemorySecretStore
) -> None:
    await add(db, seeded, "cursor-1", "cursor", LOGIN)
    await seeded.delete("cursor-1")
    with pytest.raises(NoAccountError):
        await lease(db, seeded, "executor", ["cursor"])
