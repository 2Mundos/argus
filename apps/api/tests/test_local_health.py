"""Local accounts from the Argus API process (local harness spec): discovery and
its failure classes, per-account health, the editor, the admin test and their
REST and MCP entry points. Every model server here is a fake transport."""

import json
import socket
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from api_support import insert_agent
from argus_api.accounts import local_health
from argus_api.accounts import service as accounts
from argus_api.accounts.local_health import (
    LocalAccount,
    LocalHealth,
    discover,
    probe_all,
    resolve,
    verdict,
    warnings,
)
from argus_api.db import Database
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.settings import service as settings_service
from argus_core.accounts import AccountCreate, AccountUpdate
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, register, tool

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
WORKER_URL = "http://host.docker.internal:8080"
TOKEN = "argus-local-harness"


def _dns(_: httpx.Request) -> httpx.Response:
    try:
        raise socket.gaierror(-2, "Name or service not known")
    except socket.gaierror as exc:
        raise httpx.ConnectError("[Errno -2] Name or service not known") from exc


def _refused(_: httpx.Request) -> httpx.Response:
    try:
        raise ConnectionRefusedError(111, "Connect call failed")
    except ConnectionRefusedError as exc:
        raise httpx.ConnectError("All connection attempts failed") from exc


class Servers:
    """Fake model servers keyed by `host:port`. A value is a list of model ids, or a
    handler for every request to that address. Chat answers are scripted."""

    def __init__(self) -> None:
        self.at: dict[str, list[str] | Callable[[httpx.Request], httpx.Response]] = {
            "127.0.0.1:8080": ["spark-x2.6"]
        }
        self.chat: list[tuple[int, dict]] = []
        self.seen: list[tuple[str, str, dict | None, httpx.Headers]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, str(request.url), body, request.headers))
        where = f"{request.url.host}:{request.url.port}"
        if request.url.host == "host.docker.internal" and where not in self.at:
            return _dns(request)  # the host API cannot resolve the Docker name
        served = self.at.get(where)
        if served is None:
            return _refused(request)
        if callable(served):
            return served(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in served]})
        if request.url.path == "/v1/chat/completions":
            status, payload = self.chat.pop(0)
            return httpx.Response(status, json=payload)
        return httpx.Response(404)

    def urls(self, method: str | None = None) -> list[str]:
        return [u for m, u, _, _ in self.seen if method in (None, m)]


def _account(**credential: Any) -> LocalAccount:
    found = resolve(
        "a", "spark", {"base_url": WORKER_URL, **credential}, container=False
    )
    assert found is not None
    return found


async def _discover(handler) -> local_health.Discovery:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        return await discover(client, "http://127.0.0.1:8080", TOKEN)


# -- discovery and its failure classes ---------------------------------------------


@pytest.mark.parametrize(
    ("handler", "failure", "status"),
    [
        (_dns, "dns_failed", None),
        (_refused, "connection_refused", None),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("t")), "timeout", None),
        (lambda r: httpx.Response(500, text="secret upstream text"), "http_error", 500),
        (lambda r: httpx.Response(200, text="<html>"), "invalid_response", 200),
        (lambda r: httpx.Response(200, json={"data": "x"}), "invalid_response", 200),
        (
            lambda r: httpx.Response(302, headers={"location": "http://evil.test/"}),
            "redirect_refused",
            302,
        ),
    ],
    ids=["dns", "refused", "timeout", "5xx", "not-json", "bad-shape", "redirect"],
)
async def test_discovery_failures_are_classes_never_text(
    handler, failure, status
) -> None:
    found = await _discover(handler)
    assert (found.failure, found.http_status, found.models) == (failure, status, ())
    check = found.check()
    assert check.status == "failed" and check.failure == failure
    assert "secret upstream text" not in (check.detail or "")
    assert "Errno" not in (check.detail or "")


async def test_a_redirect_is_never_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "http://169.254.169.254/"})

    found = await _discover(handler)
    assert found.failure == "redirect_refused"
    assert seen == ["http://127.0.0.1:8080/v1/models"]


async def test_discovery_reads_valid_ids_and_the_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"default_generation_settings": {"n_ctx": 32768}}
            )
        return httpx.Response(
            200, json={"data": [{"id": "qwen3:8b"}, {"id": "bad name!"}, {"x": 1}]}
        )

    found = await _discover(handler)
    assert found.ok and found.models == ("qwen3:8b",)
    assert found.window("qwen3:8b") == 32768  # llama.cpp /props, no meta
    assert found.check().status == "passed"


# -- health: endpoint and the models the account runs --------------------------------


def _found(models: tuple[str, ...], failure=None) -> local_health.Discovery:
    return local_health.Discovery(
        endpoint="http://127.0.0.1:8080",
        failure=failure,
        http_status=None,
        models=models,
        n_ctx={},
        props_n_ctx=None,
        latency_ms=1,
        at=local_health.utcnow(),
    )


@pytest.mark.parametrize(
    ("model", "override", "found", "healthy", "warned"),
    [
        ("spark", None, ("spark",), True, 0),
        (None, None, ("spark",), True, 0),  # workers run the served model
        # A stale account model behind a served orchestrator.model: available for
        # the built-in, with a warning for workers.
        ("qwen-stale", "spark", ("spark",), True, 1),
        ("spark", "gone", ("spark",), True, 1),
        ("qwen-stale", None, ("spark",), False, 1),
        ("qwen-stale", "gone", ("spark",), False, 2),
        ("spark", None, (), False, 0),
    ],
)
def test_health_follows_the_models_the_account_runs(
    model, override, found, healthy, warned
) -> None:
    account = _account(**({"model": model} if model else {}))
    ok, reason = verdict(account, override, _found(found))
    assert ok is healthy
    assert (reason == "") is healthy
    if not healthy:
        assert reason.startswith(local_health.LOCAL_UNAVAILABLE)
        assert "http://127.0.0.1:8080" in reason
    assert len(warnings(account, override, _found(found))) == warned


def test_an_unreachable_endpoint_names_its_class_and_origin() -> None:
    ok, reason = verdict(_account(), None, _found((), failure="dns_failed"))
    assert ok is False
    assert reason == "local model unreachable: dns_failed at http://127.0.0.1:8080"
    assert verdict(_account(), None, None) == (None, "")  # not probed


async def _add_local(
    db: Database, store: MemorySecretStore, name: str, **credential: Any
) -> str:
    doc = {"base_url": WORKER_URL, **credential}
    await store.put(name, doc)
    async with db.write() as tx:
        created = await accounts.create_account(
            tx,
            name,
            AccountCreate(name=name, provider="local", credential=doc),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return created.id


async def _state(db: Database, account_id: str) -> tuple[str, str | None]:
    async with db.read() as conn:
        a = await accounts.get_account(conn, account_id, db.now())
    return a.state, a.disabled_reason


@pytest.mark.parametrize(
    ("container", "probed"),
    [
        (False, ["http://127.0.0.1:8080/v1/models"]),  # host API: no hosts entry
        (True, ["http://host.docker.internal:8080/v1/models"]),  # API container
    ],
)
async def test_each_account_is_probed_on_its_own_api_endpoint(
    db: Database, tmp_path: Path, container: bool, probed: list[str]
) -> None:
    store, servers = MemorySecretStore(), Servers()
    servers.at["host.docker.internal:8080"] = ["spark-x2.6"]
    first = await _add_local(db, store, "a", model="spark-x2.6")
    second = await _add_local(db, store, "b")
    elsewhere = await _add_local(db, store, "c", api_base_url="http://127.0.0.1:1")
    health = LocalHealth(
        transport=httpx.MockTransport(servers.handle), container=container
    )
    got = await probe_all(db, store, health, tmp_path / "policy.json")
    # Two accounts on one endpoint share one request.
    listed = [u for u in servers.urls() if u.endswith("/v1/models")]
    assert listed == [*probed, "http://127.0.0.1:1/v1/models"]
    assert got[first] == (True, "") and got[second] == (True, "")
    assert got[elsewhere] == (
        False,
        "local model unreachable: connection_refused at http://127.0.0.1:1",
    )


async def test_health_disables_and_restores_only_its_own_disables(
    db: Database, tmp_path: Path
) -> None:
    store, servers = MemorySecretStore(), Servers()
    stale = await _add_local(db, store, "stale", model="qwen-stale")
    manual = await _add_local(db, store, "manual", model="qwen-stale")
    async with db.write() as tx:
        await accounts.update_account(
            tx, manual, AccountUpdate(enabled=False), "admin:test"
        )
    health = LocalHealth(transport=httpx.MockTransport(servers.handle), container=False)
    policy = tmp_path / "policy.json"

    async def apply() -> None:
        health._found.clear()
        verdicts = await probe_all(db, store, health, policy)
        async with db.write() as tx:
            await accounts.sync_local_health(tx, verdicts)

    await apply()
    state, reason = await _state(db, stale)
    assert state == "disabled"
    assert reason == (
        "local model unavailable: `qwen-stale` not served at http://127.0.0.1:8080"
    )
    # The orchestrator's model is served: the account runs it, so it is healthy.
    async with db.write() as tx:
        await settings_service.put_setting(
            tx,
            policy,
            settings_service.GLOBAL,
            accounts.ORCHESTRATOR_MODEL_KEY,
            "local/spark-x2.6",
            "admin:test",
        )
    await apply()
    assert await _state(db, stale) == ("available", None)
    # An admin's disable is never undone by a probe.
    assert (await _state(db, manual))[0] == "disabled"
    assert (await _state(db, manual))[1] == "disabled by admin:test"


# -- REST, the dashboard's contract ---------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_LOCAL_HEALTH_INTERVAL_S", "3600")
    with TestClient(app, headers=ADMIN) as c:
        app.state.secrets = MemorySecretStore()
        yield c


@pytest.fixture
def servers(client: TestClient) -> Servers:
    found = Servers()
    app.state.local_health.transport = httpx.MockTransport(found.handle)
    app.state.local_health.container = False
    return found


def _create(client: TestClient, **credential: Any) -> str:
    res = client.post(
        "/api/accounts",
        json={
            "name": "spark",
            "provider": "local",
            "credential": {"base_url": WORKER_URL, **credential},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _local_view(client: TestClient, account_id: str) -> dict:
    [found] = [a for a in client.get("/api/accounts").json() if a["id"] == account_id]
    return found["local"]


def test_the_editor_sets_validates_warns_and_resets(
    client: TestClient, servers: Servers
) -> None:
    account = _create(client, model="qwen-stale")
    view = _local_view(client, account)
    assert (view["api_endpoint"], view["api_endpoint_source"]) == (
        "http://127.0.0.1:8080",
        "derived",
    )
    assert view["discovery"]["status"] == "not_run"  # reads never probe
    assert "auth_token" not in json.dumps(view)

    # SSRF: anything but a local origin is refused on every path, unchanged.
    for bad in (
        "http://172.17.0.1:8080",
        "http://10.0.0.5:8080",
        "http://169.254.169.254",
        "http://127.0.0.1:8080/v1",
    ):
        res = client.put(f"/api/accounts/{account}/local", json={"api_base_url": bad})
        assert res.status_code == 422, (bad, res.text)
    replaced = client.put(
        f"/api/accounts/{account}/credential",
        json={"credential": {"base_url": "http://evil.test:8080"}},
    )
    assert replaced.status_code == 422, replaced.text
    assert servers.seen == []

    res = client.put(
        f"/api/accounts/{account}/local",
        json={"api_base_url": "http://localhost:8080", "context_tokens": 65536},
    )
    assert res.status_code == 200, res.text
    local = res.json()["local"]
    assert (local["api_endpoint"], local["api_endpoint_source"]) == (
        "http://localhost:8080",
        "api_base_url",
    )
    assert local["base_url"] == WORKER_URL  # workers keep theirs
    # A fresh model listing at once: the stored model is stale.
    assert local["served_models"] is None  # localhost:8080 is not a fake server
    assert local["discovery"]["failure"] == "connection_refused"
    res = client.put(f"/api/accounts/{account}/local", json={"api_base_url": None})
    local = res.json()["local"]
    assert local["api_endpoint_source"] == "derived"
    assert local["served_models"] == ["spark-x2.6"]
    assert local["discovery"]["failure"] == "model_not_served"
    assert any("Stale model" in w and "qwen-stale" in w for w in local["warnings"])
    assert servers.urls("POST") == []  # editing never infers

    reset = client.put(
        f"/api/accounts/{account}/local",
        json={"api_base_url": None, "model": None, "context_tokens": None},
    )
    local = reset.json()["local"]
    assert (local["model"], local["context_tokens"], local["warnings"]) == (
        None,
        None,
        [],
    )
    assert local["discovery"]["status"] == "passed"
    events = client.get("/api/events", params={"kind": "account.local_updated"}).json()
    items = events if isinstance(events, list) else events["items"]
    assert max(items, key=lambda e: e["id"])["data"]["fields"] == [
        "api_base_url",
        "context_tokens",
        "model",
    ]
    assert "localhost" not in json.dumps(items)  # fields, never values


def test_the_test_runs_only_what_is_asked_with_separate_statuses(
    client: TestClient, servers: Servers
) -> None:
    account = _create(client, model="qwen-stale")
    client.put("/api/settings/orchestrator.model", json={"value": "local/spark-x2.6"})
    url = f"/api/accounts/{account}/local/test"

    found = client.post(url, json={}).json()
    assert found["discovery"]["status"] == "passed"  # the orchestrator's model runs
    # Discovery never marks a completion passed, and sends none.
    assert (found["inference"]["status"], found["orchestrator"]["status"]) == (
        "not_run",
        "not_run",
    )
    assert servers.urls("POST") == []
    assert found["worker_endpoint"] == WORKER_URL
    assert "cannot test that address" in found["worker_note"]

    servers.chat = [
        (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "report_status",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
    ]
    both = client.post(url, json={"inference": True, "tool_calling": True}).json()
    # The account's own model is stale: inference is not sent, and says why.
    assert (both["inference"]["status"], both["inference"]["failure"]) == (
        "skipped",
        "model_not_served",
    )
    # The tool-call check runs on orchestrator.model, the model the built-in runs.
    assert both["orchestrator"]["status"] == "passed"
    assert both["orchestrator"]["model"] == "spark-x2.6"
    [(_, sent_to, body, headers)] = [s for s in servers.seen if s[0] == "POST"]
    assert sent_to == "http://127.0.0.1:8080/v1/chat/completions"
    assert body["model"] == "spark-x2.6" and body["tools"][0]["function"]["name"]
    assert body["max_tokens"] == local_health.TEST_MAX_TOKENS == 256
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(both)

    view = _local_view(client, account)
    assert (view["inference"]["status"], view["orchestrator"]["status"]) == (
        "skipped",
        "passed",
    )
    events = client.get("/api/events", params={"kind": "account.local_tested"}).json()
    items = events if isinstance(events, list) else events["items"]
    assert max(items, key=lambda e: e["id"])["data"]["orchestrator"] == {
        "status": "passed",
        "failure": None,
    }
    # An edit makes the last test stale: its statuses no longer apply.
    client.put(f"/api/accounts/{account}/local", json={"model": "spark-x2.6"})
    view = _local_view(client, account)
    assert (view["inference"]["status"], view["orchestrator"]["status"]) == (
        "not_run",
        "not_run",
    )


def test_inference_and_tool_failures_are_reported_apart(
    client: TestClient, servers: Servers
) -> None:
    account = _create(client, model="spark-x2.6")
    servers.chat = [
        (200, {"choices": [{"message": {"content": "OK"}}]}),
        (200, {"choices": [{"message": {"content": "I cannot call tools."}}]}),
    ]
    got = client.post(
        f"/api/accounts/{account}/local/test",
        json={"inference": True, "tool_calling": True},
    ).json()
    assert got["inference"]["status"] == "passed"
    assert got["inference"]["model"] == "spark-x2.6"
    assert (got["orchestrator"]["status"], got["orchestrator"]["failure"]) == (
        "failed",
        "no_tool_call",
    )
    assert "I cannot call tools" not in json.dumps(got)  # never the model's text
    inference_body = next(b for m, _, b, _ in servers.seen if m == "POST")
    assert inference_body["max_tokens"] == 256 and "tools" not in inference_body


@pytest.mark.parametrize(
    ("message", "finish", "cut"),
    [
        # A reasoning model that spent the budget thinking: never a pass.
        ({"content": "", "reasoning_content": "Let me think..."}, "length", True),
        ({"content": None, "reasoning_content": "Hmm"}, "stop", False),
        ({"content": "   "}, "stop", False),
    ],
    ids=["reasoning-cut", "reasoning-only", "blank"],
)
def test_an_empty_answer_never_passes(
    client: TestClient, servers: Servers, message: dict, finish: str, cut: bool
) -> None:
    account = _create(client, model="spark-x2.6")
    servers.chat = [
        (200, {"choices": [{"message": message, "finish_reason": finish}]}),
        (200, {"choices": [{"message": message, "finish_reason": finish}]}),
    ]
    got = client.post(
        f"/api/accounts/{account}/local/test",
        json={"inference": True, "tool_calling": True},
    ).json()
    assert (got["inference"]["status"], got["inference"]["failure"]) == (
        "failed",
        "empty_answer",
    )
    assert ("256 output tokens" in got["inference"]["detail"]) is cut
    assert got["orchestrator"]["status"] == "failed"
    assert got["orchestrator"]["failure"] == ("empty_answer" if cut else "no_tool_call")
    assert "think" not in json.dumps(got)  # never the model's reasoning


def test_an_unresolvable_endpoint_fails_discovery_and_sends_nothing(
    client: TestClient, servers: Servers
) -> None:
    account = _create(client, api_base_url=WORKER_URL)  # the host API cannot resolve it
    got = client.post(
        f"/api/accounts/{account}/local/test",
        json={"inference": True, "tool_calling": True},
    ).json()
    assert (got["discovery"]["status"], got["discovery"]["failure"]) == (
        "failed",
        "dns_failed",
    )
    assert "does not resolve from the Argus API process" in got["discovery"]["detail"]
    assert got["inference"]["status"] == got["orchestrator"]["status"] == "skipped"
    assert servers.urls("POST") == []


def test_only_an_admin_edits_or_tests(client: TestClient, servers: Servers) -> None:
    account = _create(client)
    orchestrator = {"Authorization": f"Bearer {register(client, 'orchestrator')}"}
    put = client.put(
        f"/api/accounts/{account}/local", json={"model": "x"}, headers=orchestrator
    )
    test = client.post(
        f"/api/accounts/{account}/local/test", json={}, headers=orchestrator
    )
    assert (put.status_code, test.status_code) == (403, 403)
    cloud = client.post(
        "/api/accounts",
        json={
            "name": "claude",
            "provider": "anthropic",
            "credential": {"api_key": "sk-ant-api03-" + "a" * 40},
        },
    ).json()["id"]
    assert client.post(f"/api/accounts/{cloud}/local/test", json={}).status_code == 422
    assert (
        client.put(f"/api/accounts/{cloud}/local", json={"model": "x"}).status_code
        == 422
    )
    assert servers.seen == []


def test_a_worker_lease_keeps_the_worker_endpoint_and_shape(
    client: TestClient, servers: Servers
) -> None:
    """Existing workers: a claim's engine lease carries the worker endpoint and only
    the keys a deployed NanoClaw accepts, whatever the API endpoint is."""
    _create(client, model="spark-x2.6", api_base_url="http://127.0.0.1:8080")
    worker = {"Authorization": f"Bearer {register(client)}"}
    client.post("/api/tasks", json={"project": "demo", "title": "t"})
    claimed = client.post(
        "/api/tasks/claim", json={"runtime_providers": ["local"]}, headers=worker
    )
    assert claimed.status_code == 200, claimed.text
    lease = claimed.json()["engine_lease"]
    assert lease["credential"] == {"base_url": WORKER_URL, "model": "spark-x2.6"}
    assert lease["settings"]["base_url"] == WORKER_URL
    task = claimed.json()
    again = client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["local"],
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    )
    assert again.status_code == 200, again.text
    assert again.json()["credential"] == lease["credential"]
    assert again.json()["settings"]["base_url"] == WORKER_URL


def test_mcp_edits_and_tests_through_the_same_routes(
    client: TestClient, servers: Servers
) -> None:
    account = _create(client, model="qwen-stale")
    edited = tool(
        client,
        ADMIN_TOKEN,
        "update_local_account",
        account_id=account,
        model="spark-x2.6",
    )
    assert (
        edited["local"]["model"] == "spark-x2.6" and edited["local"]["warnings"] == []
    )
    tested = tool(client, ADMIN_TOKEN, "test_local_account", account_id=account)
    assert tested["discovery"]["status"] == "passed"
    assert tested["inference"]["status"] == "not_run"
    reset = tool(
        client, ADMIN_TOKEN, "update_local_account", account_id=account, reset=True
    )
    assert reset["local"]["model"] is None
    listed = tool(client, ADMIN_TOKEN, "list_accounts")["accounts"]
    assert listed[0]["local"]["api_endpoint"] == "http://127.0.0.1:8080"
    refused = tool(
        client,
        register(client, "orchestrator"),
        "test_local_account",
        account_id=account,
    )
    assert "403" in refused["error"]
    assert servers.urls("POST") == []


async def test_a_builtin_agent_is_the_api_runtime(db: Database) -> None:
    await insert_agent(db, "plain")
    async with db.read() as conn:
        assert await local_health.agent_runtime(conn, "plain") == "worker"
        assert await local_health.agent_runtime(conn, "missing") == "worker"
