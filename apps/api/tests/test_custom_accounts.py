"""Custom OpenAI-compatible accounts (custom accounts spec): registration without
leaking the key, the lease settings, the probe and endpoints proxy against an
in-process fake provider, and 402/429 failover."""

import json
import logging
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from api_support import FakeClock, insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate
from argus_core.custom_accounts import chat_body
from argus_core.quota import quota_notice_from_response
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient

ADMIN_TOKEN = "t" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
KEY = "sk-or-v1-FAKEKEYFAKEKEY"
BASE = "https://openrouter.ai/api/v1"
A = "agent-a"
CONFIG = {
    "base_url": BASE,
    "label": "openrouter",
    "engine": {"model": "anthropic/claude-sonnet-4"},
    "executor": {
        "model": "deepseek/deepseek-chat",
        "routing_options": {"order": "deepinfra, together", "allow_fallbacks": False},
    },
}


class FakeProvider:
    """An in-process OpenAI-compatible server: records requests, answers by script."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.status = 200
        self.headers: dict[str, str] = {}
        self.models = ["a/one", "b/two"]

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        self.requests.append((scope["method"], scope["path"], headers, body))
        path = scope["path"]
        if self.status != 200:
            payload: object = {"error": {"message": f"echo {KEY}"}}
            status = self.status
        elif path.endswith("/endpoints"):
            status, payload = (
                200,
                {
                    "data": {
                        "endpoints": [
                            {
                                "provider_name": "DeepInfra",
                                "quantization": "fp8",
                                "context_length": 8192,
                                "pricing": {"prompt": "0.1", "completion": "0.2"},
                            }
                        ]
                    }
                },
            )
        elif path.endswith("/models"):
            status, payload = 200, {"data": [{"id": m} for m in self.models]}
        else:
            status, payload = 200, {"choices": []}
        raw = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    *[
                        (k.lower().encode(), v.encode())
                        for k, v in self.headers.items()
                    ],
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw})

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: FakeProvider
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        app.state.custom_transport = provider.transport()
        yield c
        app.state.custom_transport = None


def create(client: TestClient, name: str = "or", **extra):
    body = {
        "name": name,
        "provider": "custom",
        "credential": {"api_key": KEY},
        "config": CONFIG,
        **extra,
    }
    return client.post("/api/accounts", json=body, headers=ADMIN)


# -- registration --------------------------------------------------------------------


def test_key_is_stored_in_the_secret_store_and_never_returned(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        res = create(client)
        listed = client.get("/api/accounts", headers=ADMIN)
        one = client.get(f"/api/accounts/{res.json()['id']}", headers=ADMIN)
        events = client.get("/api/events", headers=ADMIN)
    assert res.status_code == 201, res.text
    for r in (res, listed, one, events):
        assert KEY not in r.text
    assert KEY not in caplog.text
    account = one.json()
    assert account["provider"] == "custom" and account["kind"] == "api_key"
    assert account["config"]["base_url"] == BASE
    assert app.state.secrets is not None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://openrouter.ai/api/v1",
        "https://user:pw@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?token=SECRETQUERY",
    ],
)
def test_invalid_base_url_is_rejected_and_not_echoed(
    client: TestClient, url: str
) -> None:
    res = client.post(
        "/api/accounts",
        json={
            "name": "bad",
            "provider": "custom",
            "credential": {"api_key": KEY},
            "config": {**CONFIG, "base_url": url},
        },
        headers=ADMIN,
    )
    assert res.status_code == 422
    assert (
        "SECRETQUERY" not in res.text and "pw@" not in res.text and KEY not in res.text
    )
    assert client.get("/api/accounts", headers=ADMIN).json() == []


@pytest.mark.parametrize(
    "extra",
    [
        {"credential": {"api_key": " "}},
        {"credential": {"token": KEY}},
        {"config": None},
    ],
)
def test_bad_credential_or_missing_config_is_rejected(
    client: TestClient, extra
) -> None:
    res = create(client, **extra)
    assert res.status_code == 422 and KEY not in res.text


def test_config_is_updatable_and_only_for_custom(client: TestClient) -> None:
    account = create(client).json()
    patched = client.patch(
        f"/api/accounts/{account['id']}",
        json={"config": {**CONFIG, "engine": {"model": "x/y"}}},
        headers=ADMIN,
    )
    assert patched.status_code == 200
    assert patched.json()["config"]["engine"]["model"] == "x/y"
    other = client.post(
        "/api/accounts",
        json={
            "name": "claude",
            "provider": "anthropic",
            "kind": "api_key",
            "credential": {"api_key": "sk-ant-" + "api03-" + "FAKEKEY" * 8},
        },
        headers=ADMIN,
    ).json()
    res = client.patch(
        f"/api/accounts/{other['id']}", json={"config": CONFIG}, headers=ADMIN
    )
    assert res.status_code == 422


# -- probe and endpoints proxy -------------------------------------------------------


def test_probe_calls_models_with_the_key_and_returns_suggestions(
    client: TestClient, provider: FakeProvider
) -> None:
    account = create(client).json()
    res = client.post(f"/api/accounts/{account['id']}/probe", headers=ADMIN)
    assert res.json() == {
        "status": "ok",
        "http_status": 200,
        "reason": "ok",
        "models": ["a/one", "b/two"],
        "exhausted_until": None,
    }
    method, path, headers, _ = provider.requests[-1]
    assert (method, path) == ("GET", "/api/v1/models")
    assert headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in res.text


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(401, "unauthorized"), (402, "exhausted"), (429, "exhausted"), (500, "failed")],
)
def test_probe_reports_a_fixed_reason_and_no_upstream_text(
    client: TestClient, provider: FakeProvider, status: int, outcome: str
) -> None:
    account = create(client).json()
    provider.status = status
    provider.headers = {"Retry-After": "60"}
    res = client.post(f"/api/accounts/{account['id']}/probe", headers=ADMIN)
    assert res.json()["status"] == outcome
    assert KEY not in res.text and "echo" not in res.text
    # The probe reports; it does not change the account.
    got = client.get(f"/api/accounts/{account['id']}", headers=ADMIN).json()
    assert got["state"] == "available"


def test_probe_needs_admin(client: TestClient) -> None:
    account = create(client).json()
    assert client.post(f"/api/accounts/{account['id']}/probe").status_code in (401, 403)


def test_endpoints_proxy_lists_hosts_without_sending_the_key(
    client: TestClient, provider: FakeProvider
) -> None:
    account = create(client).json()
    res = client.get(
        f"/api/accounts/{account['id']}/models/deepseek/deepseek-chat/endpoints",
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    [e] = res.json()["endpoints"]
    assert (e["provider_name"], e["quantization"], e["context_length"]) == (
        "DeepInfra",
        "fp8",
        8192,
    )
    _, path, headers, _ = provider.requests[-1]
    assert path == "/api/v1/models/deepseek/deepseek-chat/endpoints"
    assert "authorization" not in headers


@pytest.mark.parametrize("author", ["..", "a%2Fb", "-x", "a b"])
def test_endpoints_proxy_validates_author_and_slug(
    client: TestClient, provider: FakeProvider, author: str
) -> None:
    account = create(client).json()
    res = client.get(
        f"/api/accounts/{account['id']}/models/{author}/slug/endpoints", headers=ADMIN
    )
    assert res.status_code in (404, 422)
    assert provider.requests == []


def test_endpoints_proxy_maps_upstream_failures(
    client: TestClient, provider: FakeProvider
) -> None:
    account = create(client).json()
    url = f"/api/accounts/{account['id']}/models/a/b/endpoints"
    provider.status = 500
    res = client.get(url, headers=ADMIN)
    assert res.status_code == 502 and KEY not in res.text and "echo" not in res.text
    provider.status = 404
    assert client.get(url, headers=ADMIN).status_code == 404


def test_endpoints_proxy_is_openrouter_only(client: TestClient) -> None:
    other = {"base_url": "http://localhost:8000/v1", "label": "vllm"}
    account = create(client, "local", config=other).json()
    res = client.get(
        f"/api/accounts/{account['id']}/models/a/b/endpoints", headers=ADMIN
    )
    assert res.status_code == 422


# -- lease, request body and failover ------------------------------------------------


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


async def add(db: Database, store: MemorySecretStore, name: str, **kw) -> str:
    await store.put(name, {"api_key": KEY})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(
                name=name,
                provider="custom",
                secret_ref=f"sops:{name}",
                config=CONFIG,  # type: ignore[arg-type]
                **kw,
            ),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def claimed(db: Database):
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "admin:test")
        task = await tasks.claim(tx, A, [])
    assert task is not None
    return task


async def lease(db: Database, store, role="executor"):
    task = await claimed(db)
    async with db.write() as tx:
        return await accounts.lease(
            tx, A, ["custom"], store, role, task_id=task.id, attempt=task.attempt
        )


async def test_lease_carries_the_role_settings_and_the_request_reaches_the_provider(
    db: Database, store: MemorySecretStore, provider: FakeProvider
) -> None:
    await insert_agent(db, A)
    await add(db, store, "or")
    got = await lease(db, store, "executor")
    assert got.provider == "custom" and got.settings is not None
    assert got.settings.model == "deepseek/deepseek-chat"
    assert got.settings.provider == {
        "order": ["deepinfra", "together"],
        "only": ["deepinfra", "together"],
        "allow_fallbacks": False,
    }
    assert "settings" in got.model_dump()

    async with httpx.AsyncClient(
        transport=provider.transport(), base_url="https://openrouter.ai"
    ) as http:
        await http.post(
            "/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {got.credential['api_key']}"},
            json=chat_body(got.settings, [{"role": "user", "content": "hi"}]),
        )
    _, path, headers, body = provider.requests[-1]
    sent = json.loads(body)
    assert path == "/api/v1/chat/completions"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert sent["model"] == "deepseek/deepseek-chat"
    assert sent["provider"]["order"] == ["deepinfra", "together"]


async def test_each_role_has_its_own_model(
    db: Database, store: MemorySecretStore
) -> None:
    await insert_agent(db, A)
    await add(db, store, "or")
    got = await lease(db, store, "engine")
    assert got.settings is not None
    assert (got.settings.model, got.settings.provider) == (
        "anthropic/claude-sonnet-4",
        None,
    )


@pytest.mark.parametrize(
    ("status", "headers"),
    [(429, {"Retry-After": "600"}), (402, {})],
)
async def test_402_and_429_exhaust_the_account_and_fail_over_without_an_attempt(
    db: Database,
    store: MemorySecretStore,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    status: int,
    headers: dict[str, str],
) -> None:
    await insert_agent(db, A)
    a = await add(db, store, "or-a", priority=100)
    await add(db, store, "or-b", priority=90)
    held = await lease(db, store, "engine")
    assert held.name == "or-a"
    task = await claimed(db)  # a second claim cannot happen; reuse the first task below
    del task
    async with db.read() as conn, conn.execute("SELECT id, attempt FROM tasks") as cur:
        rows = await cur.fetchall()
    task_id, attempt = rows[0][0], rows[0][1]

    notice = quota_notice_from_response(status, headers, clock.now)
    assert notice is not None and KEY not in notice
    with caplog.at_level(logging.INFO):
        async with db.write() as tx:
            after = await accounts.fail_task(
                tx,
                A,
                task_id,
                attempt,
                notice,
                "provider_failure",
                store,
                timedelta(hours=1),
            )
    assert (after.state, after.failures) == (S.READY, 0)
    assert after.failover is not None and after.failover.to_account == "or-b"
    async with db.read() as conn:
        acct = await accounts.get_account(conn, a, clock.now)
    assert acct.state == "exhausted"
    expected = clock.now + timedelta(seconds=600) if headers else None
    if expected:
        assert acct.exhausted_until == expected
    assert KEY not in caplog.text
