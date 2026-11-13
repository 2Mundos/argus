"""Integrations (docs/specs/integrations.spec.md): admin-only management, project
isolation, secrets only in the store, validation without real network, SOPS/DB
consistency, and that no worker ever receives a secret. No real MCP server or
cluster is contacted: a mock transport and resolver stand in for both."""

import asyncio
import base64
import json
import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from api_support import insert_project
from argus_api.db import Database
from argus_api.integrations import checks, service
from argus_api.main import app
from argus_api.secrets import MemorySecretStore, SecretStoreError, SopsSecretStore
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "i" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
SECRET = "mcp-secret-ZQ7xK2v9-never-shown"
ROTATED = "mcp-secret-rotated-W4pL8n-never-shown"
MCP_URL = "https://mcp.example.test/mcp"
K8S = {"server": "https://k8s.example.test:6443", "namespace": "team-a"}
# Permission keys: namespace:verb:group:resource:subresource; empty group is core.
LIST = "team-a:list::pods:"
NAMESPACE_PERMISSIONS = {
    LIST,
    "team-a:list:apps:deployments:",
    "team-a:create::pods:exec",
}


class Upstream:
    """Fake MCP server and Kubernetes API; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.mcp_status = 200
        self.allowed: set[str] = {LIST}
        self.review_overrides: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/mcp":
            # A hostile server echoing the token must not get it into a detail.
            body = json.loads(request.content)
            result = (
                {
                    "resultType": "complete",
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {},
                }
                if body["method"] == "server/discover"
                else {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1"},
                }
            )
            echo = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": result,
                "echo": request.headers.get("authorization", ""),
            }
            if self.mcp_status == 302:
                return httpx.Response(302, headers={"Location": "https://evil.test/x"})
            return httpx.Response(self.mcp_status, json=echo)
        attrs = json.loads(request.content)["spec"]["resourceAttributes"]
        key = ":".join(
            (
                attrs.get("namespace", "*"),
                attrs["verb"],
                attrs.get("group", ""),
                attrs["resource"],
                attrs.get("subresource", ""),
            )
        )
        return httpx.Response(
            201,
            json=self.review_overrides.get(
                key, {"status": {"allowed": key in self.allowed}}
            ),
        )


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: Upstream
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers=ADMIN) as c:
        app.state.secrets = MemorySecretStore()
        app.state.integration_transport = httpx.MockTransport(upstream.handler)
        app.state.integration_resolver = _public
        for slug in ("alpha", "beta"):
            assert (
                c.put(f"/api/projects/{slug}", json={"name": slug}).status_code == 200
            )
        yield c
    del app.state.integration_transport
    del app.state.integration_resolver


async def _public(host: str, port: int) -> list[str]:
    return ["203.0.113.10"]


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


def mcp(
    client: TestClient, project: str = "alpha", name: str = "docs", **extra
) -> dict:
    body = {
        "kind": "mcp",
        "name": name,
        "config": {"url": MCP_URL},
        "secret": {"token": SECRET},
        **extra,
    }
    res = client.post(f"/api/projects/{project}/integrations", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def k8s(client: TestClient, access: str = "read_only") -> dict:
    body = {"kind": "kubernetes", "name": "cluster", "access": access, "config": K8S}
    res = client.post(
        "/api/projects/alpha/integrations", json={**body, "secret": {"token": SECRET}}
    )
    assert res.status_code == 201, res.text
    return res.json()


def act(client: TestClient, item: dict, action: str, project: str | None = None, **kw):
    path = (
        f"/api/projects/{project or item['project']}/integrations/{item['id']}/{action}"
    )
    return client.post(path, **kw)


def claim(client: TestClient, worker: dict[str, str], project: str = "alpha") -> dict:
    assert (
        client.post("/api/tasks", json={"project": project, "title": "t"}).status_code
        == 201
    )
    res = client.post("/api/tasks/claim", json={}, headers=worker)
    assert res.status_code == 200, res.text
    return res.json()


def no_secret_anywhere(client: TestClient, tmp_path: Path, *secrets: str) -> None:
    events = client.get("/api/events", params={"limit": 500}).text
    for secret in secrets:
        assert secret not in events
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes(), path


def test_admin_creates_and_reads_without_the_secret(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    created = mcp(client)
    assert created["has_secret"] and created["state"] == "active"
    assert created["validation"] == "unchecked"
    assert created["access"] == "read_only"
    listed = client.get("/api/integrations")
    one = client.get(f"/api/projects/alpha/integrations/{created['id']}")
    for text in (json.dumps(created), listed.text, one.text):
        assert SECRET not in text
    assert app.state.secrets.get(f"integration-{created['id']}") == {
        "token": SECRET,
        "version": 1,
    }
    no_secret_anywhere(client, tmp_path, SECRET)
    assert SECRET not in caplog.text
    kinds = [e["kind"] for e in client.get("/api/events", params={"limit": 50}).json()]
    assert "integration.created" in kinds


def test_only_admins_manage(client: TestClient) -> None:
    item = mcp(client)
    paths = [
        ("GET", "/api/integrations", None),
        ("GET", "/api/projects/alpha/integrations", None),
        ("POST", "/api/projects/alpha/integrations", {"kind": "mcp"}),
        ("GET", f"/api/projects/alpha/integrations/{item['id']}", None),
        ("POST", f"/api/projects/alpha/integrations/{item['id']}/validate", None),
        ("POST", f"/api/projects/alpha/integrations/{item['id']}/rotate", {"secret": {"token": "x"}}),
        ("POST", f"/api/projects/alpha/integrations/{item['id']}/revoke", None),
        ("PATCH", f"/api/projects/alpha/integrations/{item['id']}", {"expected_version": item["version"], "access": "read_write"}),
    ]  # fmt: skip
    for role in ("worker", "orchestrator"):
        headers = register(client, role)
        for method, path, body in paths:
            res = client.request(method, path, json=body, headers=headers)
            assert res.status_code == 403, (role, path)
    for method, path, body in paths:
        res = client.request(method, path, json=body, headers={"Authorization": ""})
        assert res.status_code == 401, path
    assert (
        client.get(f"/api/projects/alpha/integrations/{item['id']}").json()["state"]
        == "active"
    )


def test_another_projects_id_is_not_found(client: TestClient) -> None:
    item = mcp(client, "alpha")
    path = f"/api/projects/beta/integrations/{item['id']}"
    assert client.get(path).status_code == 404
    for action, kw in (
        ("validate", {}),
        ("rotate", {"json": {"secret": {"token": ROTATED}}}),
        ("revoke", {}),
    ):
        assert act(client, item, action, project="beta", **kw).status_code == 404, (
            action
        )
    edit = client.patch(path, json={"expected_version": item["version"]})
    assert edit.status_code == 404
    assert client.get("/api/projects/beta/integrations").json() == []
    assert app.state.secrets.get(f"integration-{item['id']}") == {
        "token": SECRET,
        "version": 1,
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"config": {"url": "http://mcp.example.test/mcp"}}, "config_invalid"),
        ({"config": {"url": "https://user:pw@mcp.example.test/mcp"}}, "config_invalid"),
        ({"config": {"url": f"{MCP_URL}?token=abc"}}, "config_invalid"),
        ({"config": {"url": "https://127.0.0.1/mcp"}}, "config_invalid"),
        ({"config": {"url": "https://169.254.169.254/latest"}}, "config_invalid"),
        ({"config": {"url": "https://localhost/mcp"}}, "config_invalid"),
        ({"config": {"url": "https://[::1]/mcp"}}, "config_invalid"),
        ({"config": {"url": MCP_URL, "headers": {"X": "y"}}}, "config_invalid"),
        ({"name": "Bad Name"}, "name_invalid"),
        ({"secret": {"token": f"{SECRET} with spaces"}}, "secret_invalid"),
        (
            {"kind": "kubernetes", "config": {**K8S, "namespace": "Team_A"}},
            "config_invalid",
        ),
        (
            {"kind": "kubernetes", "config": {**K8S, "ca_data": "bm90IGEgY2VydA=="}},
            "config_invalid",
        ),
    ],
)
def test_invalid_input_is_refused_without_echo(
    client: TestClient, body: dict, code: str
) -> None:
    full = {
        "kind": "mcp",
        "name": "docs",
        "config": {"url": MCP_URL},
        "secret": {"token": SECRET},
    }
    res = client.post("/api/projects/alpha/integrations", json={**full, **body})
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == code
    assert SECRET not in res.text
    assert client.get("/api/integrations").json() == []
    assert app.state.secrets.names() == set()


def test_scope_must_be_a_real_project_and_not_global(client: TestClient) -> None:
    body = {
        "kind": "mcp",
        "name": "docs",
        "config": {"url": MCP_URL},
        "secret": {"token": SECRET},
    }
    assert (
        client.post("/api/projects/global/integrations", json=body).status_code == 422
    )
    assert client.post("/api/projects/nope/integrations", json=body).status_code == 404
    bad = client.post(
        "/api/projects/alpha/integrations", json={**body, "access": "admin"}
    )
    assert bad.status_code == 422 and SECRET not in bad.text
    assert app.state.secrets.names() == set()
    mcp(client)
    assert client.post("/api/projects/alpha/integrations", json=body).status_code == 409
    assert len(app.state.secrets.names()) == 1


def test_mcp_validation_outcomes_never_quote_the_server(
    client: TestClient, upstream: Upstream
) -> None:
    item = mcp(client)
    ok = act(client, item, "validate").json()
    assert ok["validation"] == "ok" and ok["validated_at"]
    sent = upstream.requests[-1]
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    assert json.loads(sent.content)["method"] == "server/discover"
    upstream.mcp_status = 401
    failed = act(client, item, "validate").json()
    assert failed["validation"] == "failed" and "401" in failed["validation_detail"]
    assert SECRET not in json.dumps(failed)


def test_validation_does_not_follow_redirects(
    client: TestClient, upstream: Upstream
) -> None:
    item = mcp(client)
    upstream.mcp_status = 302
    res = act(client, item, "validate").json()
    assert res["validation"] == "failed" and "302" in res["validation_detail"]
    assert len(upstream.requests) == 1  # the Location was not followed


def test_validation_refuses_hosts_resolving_to_internal_addresses(
    client: TestClient, upstream: Upstream
) -> None:
    item = mcp(client)

    async def rebinding(host: str, port: int) -> list[str]:
        return ["203.0.113.10", "169.254.169.254"]

    app.state.integration_resolver = rebinding
    res = act(client, item, "validate").json()
    assert res["validation"] == "failed" and "link-local" in res["validation_detail"]
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("access", "allowed", "status", "detail"),
    [
        ("read_only", set(), "failed", "cannot list pods"),
        ("read_only", {LIST, "*:list::pods:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:list::pods:"}, "failed", "too broad"),
        ("read_only", {LIST, "*:get::secrets:"}, "failed", "too broad"),
        ("read_only", {LIST, "*:list::secrets:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:watch::secrets:"}, "failed", "too broad"),
        ("read_only", {LIST, "*:get::configmaps:"}, "failed", "too broad"),
        ("read_only", {LIST, "*:list:apps:deployments:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:get:apps:statefulsets:"}, "failed", "too broad"),
        ("read_only", {LIST, "*:watch:apps:daemonsets:"}, "failed", "too broad"),
        ("read_only", {LIST, "*:list:batch:jobs:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:get:batch:cronjobs:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:create::pods:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:patch::pods:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:delete::pods:"}, "failed", "too broad"),
        ("read_write", {LIST, "*:create::pods:exec"}, "failed", "too broad"),
        ("read_write", {LIST, "*:create::pods:attach"}, "failed", "too broad"),
        ("read_write", {LIST, "*:create::pods:portforward"}, "failed", "too broad"),
        (
            "read_write",
            {LIST, "*:create::serviceaccounts:token"},
            "failed",
            "too broad",
        ),
        (
            "read_only",
            {LIST, "*:create:rbac.authorization.k8s.io:clusterrolebindings:"},
            "failed",
            "too broad",
        ),
        ("read_write", {LIST, "*:*:*:*:"}, "failed", "too broad"),
        ("read_only", {LIST, "team-a:create::pods:"}, "failed", "not read-only"),
        ("read_only", {LIST}, "ok", "explicit checks passed"),
        ("read_write", NAMESPACE_PERMISSIONS, "ok", "explicit checks passed"),
        (
            "read_write",
            NAMESPACE_PERMISSIONS | {"*:list:apps:deployments:"},
            "failed",
            "too broad",
        ),
        (
            "read_write",
            NAMESPACE_PERMISSIONS | {"*:create::pods:exec"},
            "failed",
            "too broad",
        ),
        ("read_write", {LIST, "team-a:create::pods:"}, "ok", "explicit checks passed"),
    ],
)
def test_kubernetes_validation_checks_explicit_permissions(
    client: TestClient,
    upstream: Upstream,
    access: str,
    allowed: set[str],
    status: str,
    detail: str,
) -> None:
    item = k8s(client, access)
    upstream.allowed = allowed
    res = act(client, item, "validate").json()
    assert res["validation"] == status and detail in res["validation_detail"], res
    assert SECRET not in res["validation_detail"]
    attributes = [
        json.loads(request.content)["spec"]["resourceAttributes"]
        for request in upstream.requests
    ]
    for permission, expected in (
        ("*:list::pods:", {"verb": "list", "group": "", "resource": "pods"}),
        (
            "*:list:apps:deployments:",
            {"verb": "list", "group": "apps", "resource": "deployments"},
        ),
        (
            "*:list:batch:jobs:",
            {"verb": "list", "group": "batch", "resource": "jobs"},
        ),
        (
            "*:create:rbac.authorization.k8s.io:clusterrolebindings:",
            {
                "verb": "create",
                "group": "rbac.authorization.k8s.io",
                "resource": "clusterrolebindings",
            },
        ),
        (
            "*:create::pods:exec",
            {"verb": "create", "resource": "pods", "subresource": "exec"},
        ),
        ("*:*:*:*:", {"verb": "*", "group": "*", "resource": "*"}),
    ):
        if permission in allowed:
            assert attributes[-1] == expected
        if allowed == NAMESPACE_PERMISSIONS:
            assert expected in attributes
    paths = {r.url.path for r in upstream.requests}
    assert paths == {"/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"}


def test_rotate_replaces_the_secret_and_resets_validation(
    client: TestClient, tmp_path: Path
) -> None:
    item = mcp(client)
    act(client, item, "validate")
    res = act(client, item, "rotate", json={"secret": {"token": ROTATED}})
    assert res.status_code == 200
    rotated = res.json()
    assert rotated["validation"] == "unchecked" and rotated["rotated_at"]
    assert app.state.secrets.get(f"integration-{item['id']}") == {
        "token": ROTATED,
        "version": 2,
    }
    no_secret_anywhere(client, tmp_path, SECRET, ROTATED)


def test_revoke_deletes_the_secret_and_is_final(
    client: TestClient, tmp_path: Path
) -> None:
    item = mcp(client)
    res = act(client, item, "revoke")
    assert res.status_code == 200
    revoked = res.json()
    assert revoked["state"] == "revoked" and not revoked["has_secret"]
    assert revoked["revoked_by"]
    assert app.state.secrets.names() == set()
    for action, kw in (
        ("revoke", {}),
        ("validate", {}),
        ("rotate", {"json": {"secret": {"token": ROTATED}}}),
    ):
        conflict = act(client, item, action, **kw)
        assert (
            conflict.status_code == 409
            and conflict.json()["detail"]["code"] == "integration_revoked"
        )
    assert app.state.secrets.names() == set()
    # The name is free again: restoring means adding a new one.
    assert mcp(client)["id"] != item["id"]
    no_secret_anywhere(client, tmp_path, SECRET, ROTATED)


def edit(client: TestClient, item: dict, **body) -> httpx.Response:
    return client.patch(
        f"/api/projects/{item['project']}/integrations/{item['id']}",
        json={"expected_version": item["version"], **body},
    )


def test_edit_corrects_the_config_and_keeps_the_secret(
    client: TestClient,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    upstream: Upstream,
) -> None:
    caplog.set_level(logging.DEBUG)
    item = k8s(client)
    validated = act(client, item, "validate").json()
    assert validated["validation"] == "ok" and validated["version"] != item["version"]
    # Saving what was read changes nothing and records nothing.
    same = edit(client, validated, config=K8S, access="read_only")
    assert same.status_code == 200 and same.json() == validated
    res = edit(
        client,
        validated,
        config={"server": "https://k8s-2.example.test", "namespace": "team-b"},
        access="read_write",
    )
    assert res.status_code == 200, res.text
    after = res.json()
    assert after["id"] == item["id"] and after["has_secret"]
    assert after["config"]["server"] == "https://k8s-2.example.test"
    assert (after["access"], after["config_version"]) == ("read_write", 2)
    assert (after["validation"], after["validation_detail"], after["validated_at"]) == (
        "unchecked",
        None,
        None,
    )
    assert after["version"] not in (item["version"], validated["version"])
    assert after["rotated_at"] is None
    assert app.state.secrets.get(f"integration-{item['id']}") == {
        "token": SECRET,
        "version": 1,
    }
    # The corrected record validates against the new target with the same token.
    upstream.requests.clear()
    upstream.allowed = {"team-b:list::pods:", "team-b:create::pods:"}
    assert act(client, after, "validate").json()["validation"] == "ok"
    assert {r.headers["host"] for r in upstream.requests} == {"k8s-2.example.test"}
    assert {r.headers["authorization"] for r in upstream.requests} == {
        f"Bearer {SECRET}"
    }
    # A CA bundle is public but long: the audit names it by digest.
    ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nx\n").decode()
    current = client.get(f"/api/projects/alpha/integrations/{item['id']}").json()
    with_ca = edit(client, current, config={**current["config"], "ca_data": ca})
    assert with_ca.json()["config_version"] == 3
    updates = [
        e["data"]
        for e in client.get("/api/events", params={"limit": 50}).json()
        if e["kind"] == "integration.updated"
    ]
    by_version = {u["config_version"]: u["changes"] for u in updates}
    assert by_version[2] == {
        "server": {"before": K8S["server"], "after": "https://k8s-2.example.test"},
        "namespace": {"before": "team-a", "after": "team-b"},
        "access": {"before": "read_only", "after": "read_write"},
    }
    assert by_version[3]["ca_data"]["before"] is None
    assert by_version[3]["ca_data"]["after"].startswith("sha256:")
    assert ca not in json.dumps(updates)
    no_secret_anywhere(client, tmp_path, SECRET)
    assert SECRET not in caplog.text and SECRET not in res.text


def test_edit_needs_the_version_read_and_an_active_record(client: TestClient) -> None:
    item = mcp(client)
    assert act(client, item, "validate").status_code == 200
    stale = edit(client, item, access="read_write")
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_version"
    current = client.get(f"/api/projects/alpha/integrations/{item['id']}").json()
    assert current["access"] == "read_only" and current["validation"] == "ok"
    first = edit(client, current, access="read_write")
    assert first.status_code == 200
    # A second admin who read the same version loses the race instead of overwriting.
    assert edit(client, current, access="read_only").status_code == 409
    assert act(client, item, "revoke").status_code == 200
    revoked = client.get(f"/api/projects/alpha/integrations/{item['id']}").json()
    gone = edit(client, revoked, config={"url": "https://other.example.test/mcp"})
    assert gone.status_code == 409
    assert gone.json()["detail"]["code"] == "integration_revoked"
    assert edit(client, {**revoked, "id": "nope"}).status_code == 404


@pytest.mark.parametrize(
    "config",
    [
        {"url": "http://mcp.example.test/mcp"},
        {"url": "https://user:pw@mcp.example.test/mcp"},
        {"url": f"{MCP_URL}?token=abc"},
        {"url": f"{MCP_URL}#frag"},
        {"url": "https://127.0.0.1/mcp"},
        {"url": "https://169.254.169.254/latest"},
        {"url": "https://[::ffff:169.254.169.254]/latest"},
        {"url": "https://0.0.0.0/mcp"},
        {"url": "https://localhost/mcp"},
        {"url": "file:///etc/passwd"},
        {},
        {"url": MCP_URL, "headers": {"X": "y"}},
        {"url": MCP_URL, "tools": []},
        {"url": MCP_URL, "tools": ["ok", "has space"]},
        {"url": MCP_URL, "tools": "search"},
        {"url": MCP_URL, "tools": ["x" * 129]},
    ],
)
def test_edit_checks_the_config_as_create_does(
    client: TestClient, upstream: Upstream, config: dict
) -> None:
    item = mcp(client)
    res = edit(client, item, config=config)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "config_invalid"
    assert client.get(f"/api/projects/alpha/integrations/{item['id']}").json() == item
    assert upstream.requests == []


def test_mcp_tool_allowlist_decides_what_a_worker_could_use(
    client: TestClient,
) -> None:
    read_only = mcp(client)
    assert read_only["tool_policy"] == "unknown"
    assert mcp(client, name="rw", access="read_write")["tool_policy"] == "all"
    allowed = edit(
        client,
        read_only,
        config={"url": MCP_URL, "tools": ["search", "fetch", "search"]},
    ).json()
    assert allowed["config"]["tools"] == ["fetch", "search"]
    assert allowed["tool_policy"] == "allowlist"
    # Removing the allowlist from a read_only record makes it unusable again.
    cleared = edit(client, allowed, config={"url": MCP_URL}).json()
    assert cleared["tool_policy"] == "unknown" and "tools" not in cleared["config"]
    assert k8s(client)["tool_policy"] is None
    created = client.post(
        "/api/projects/alpha/integrations",
        json={
            "kind": "mcp",
            "name": "listed",
            "config": {"url": MCP_URL, "tools": ["read_page"]},
            "secret": {"token": SECRET},
        },
    ).json()
    assert created["tool_policy"] == "allowlist"


class FailingStore(MemorySecretStore):
    """Applies the change, then fails: the worst case for SOPS/SQLite consistency."""

    fail_on: str = ""

    async def put(self, name: str, value: dict[str, Any]) -> None:
        await super().put(name, value)
        if self.fail_on == "put":
            raise SecretStoreError("sops set failed")

    async def delete(self, name: str) -> None:
        await super().delete(name)
        if self.fail_on == "delete":
            raise SecretStoreError("sops unset failed")


def test_a_failed_store_change_rolls_back_both_sides(client: TestClient) -> None:
    store = FailingStore()
    app.state.secrets = store
    item = mcp(client)
    store.fail_on = "delete"
    assert act(client, item, "revoke").status_code == 503
    store.fail_on = "put"
    assert (
        act(client, item, "rotate", json={"secret": {"token": ROTATED}}).status_code
        == 503
    )
    after = client.get(f"/api/projects/alpha/integrations/{item['id']}").json()
    assert (
        after["state"] == "active"
        and after["has_secret"]
        and after["rotated_at"] is None
    )
    assert store.get(f"integration-{item['id']}") == {"token": SECRET, "version": 1}
    # A create whose store write fails leaves neither a row nor an entry.
    res = client.post(
        "/api/projects/alpha/integrations",
        json={
            "kind": "mcp",
            "name": "other",
            "config": {"url": MCP_URL},
            "secret": {"token": ROTATED},
        },
    )
    assert res.status_code == 503 and ROTATED not in res.text
    assert [i["name"] for i in client.get("/api/integrations").json()] == ["docs"]
    assert store.names() == {f"integration-{item['id']}"}


async def test_create_removes_the_secret_when_the_row_fails(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await insert_project(db, "alpha")
    store = MemorySecretStore()

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(service, "record", broken)
    body = service.IntegrationCreate.model_validate(
        {
            "kind": "mcp",
            "name": "docs",
            "config": {"url": MCP_URL},
            "secret": {"token": SECRET},
        }
    )
    with pytest.raises(RuntimeError):
        await service.create(db, store, "alpha", body, "admin:test")
    assert store.names() == set()
    assert await service.list_all(db) == []


async def test_a_rotation_during_validation_voids_its_result(db: Database) -> None:
    await insert_project(db, "alpha")
    store = MemorySecretStore()
    body = service.IntegrationCreate.model_validate(
        {
            "kind": "mcp",
            "name": "docs",
            "config": {"url": MCP_URL},
            "secret": {"token": SECRET},
        }
    )
    item = await service.create(db, store, "alpha", body, "admin:test")
    rotate = service.IntegrationRotate.model_validate({"secret": {"token": ROTATED}})

    async def rotating(host: str, port: int) -> list[str]:
        await service.rotate(db, store, "alpha", item.id, rotate, "admin:test")
        return ["203.0.113.10"]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with pytest.raises(service.IntegrationChangedError):
        await service.validate(
            db, store, "alpha", item.id, "admin:test", transport, rotating
        )
    after = await service.get(db, "alpha", item.id)
    assert after.validation == "unchecked"


async def test_a_config_edit_during_validation_voids_its_result(db: Database) -> None:
    await insert_project(db, "alpha")
    store = MemorySecretStore()
    body = service.IntegrationCreate.model_validate(
        {
            "kind": "mcp",
            "name": "docs",
            "config": {"url": MCP_URL},
            "secret": {"token": SECRET},
        }
    )
    item = await service.create(db, store, "alpha", body, "admin:test")
    moved = service.IntegrationUpdate(
        expected_version=item.version, config={"url": "https://other.example.test/mcp"}
    )

    async def editing(host: str, port: int) -> list[str]:
        await service.update(db, "alpha", item.id, moved, "admin:test")
        return ["203.0.113.10"]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with pytest.raises(service.IntegrationChangedError):
        await service.validate(
            db, store, "alpha", item.id, "admin:test", transport, editing
        )
    after = await service.get(db, "alpha", item.id)
    assert (after.validation, after.config_version) == ("unchecked", 2)


def _worker(client: TestClient) -> tuple[str, dict[str, str]]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return res.json()["agent"]["id"], {"Authorization": f"Bearer {res.json()['token']}"}


def _lease_claude(client: TestClient, agent_id: str, task: dict) -> None:
    from argus_api.accounts import service as accounts
    from argus_core.accounts import AccountCreate

    store, db = app.state.secrets, app.state.db

    async def lease() -> None:
        await store.put("acc-claude", {"api_key": "key-claude"})
        body = AccountCreate(
            name="claude", provider="anthropic", secret_ref="sops:acc-claude"
        )
        async with db.write() as tx:
            await accounts.create_account(
                tx, "acc-claude", body, "sops:acc-claude", "admin:test", store
            )
            await accounts.lease(
                tx,
                agent_id,
                ["anthropic"],
                store,
                task_id=task["id"],
                attempt=task["attempt"],
            )

    assert client.portal is not None
    client.portal.call(lease)


def test_only_the_opted_in_attempts_worker_gets_the_secret(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    item = k8s(client)
    assert act(client, item, "validate").json()["validation"] == "ok"
    body = {"project": "alpha", "title": "t", "integrations": [item["id"]]}
    res = client.post("/api/tasks", json=body, headers=register(client, "orchestrator"))
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "integration_admin_only"
    # A worker holding a claim without opt-in gets nothing.
    _, plain = _worker(client)
    plain_task = claim(client, plain, "alpha")
    cred = f"/api/tasks/{plain_task['id']}/integrations/{item['id']}/credential"
    res = client.post(cred, json={"attempt": 1}, headers=plain)
    assert res.json()["detail"]["code"] == "integration_not_selected"
    # The admin opts a task in; only its assignee's current attempt is served.
    task = client.post("/api/tasks", json={"project": "alpha", "title": "k"}).json()
    res = client.put(
        f"/api/tasks/{task['id']}/integrations",
        json={"expected_version": task["version"], "integrations": [item["id"]]},
    )
    assert res.status_code == 200 and res.json()["integrations"] == [item["id"]]
    agent_id, worker = _worker(client)
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == task["id"]
    _lease_claude(client, agent_id, claimed)
    status = f"/api/tasks/{task['id']}/integrations/status"
    cred = f"/api/tasks/{task['id']}/integrations/{item['id']}/credential"
    for headers in (ADMIN, register(client, "orchestrator")):
        assert (
            client.post(cred, json={"attempt": 1}, headers=headers).status_code == 403
        )
        assert (
            client.get(status, params={"attempt": 1}, headers=headers).status_code
            == 403
        )
    res = client.post(cred, json={"attempt": 1}, headers=plain)
    assert res.json()["detail"]["code"] == "not_assignee"
    res = client.get(status, params={"attempt": 1}, headers=worker)
    assert res.json()[0]["readiness"] == "ready" and SECRET not in res.text
    res = client.post(cred, json={"attempt": 1, "secret_version": 1}, headers=worker)
    assert res.status_code == 200 and res.json()["token"] == SECRET
    assert res.headers["cache-control"] == "no-store"
    paths = app.openapi()["paths"]
    assert not any(p.startswith("/api/tasks/{task_id}/integrations/") for p in paths)
    # Everything else stays secret-free: records, task, events, files, logs.
    for path in (
        "/api/integrations",
        f"/api/projects/alpha/integrations/{item['id']}",
        f"/api/tasks/{task['id']}",
    ):
        assert SECRET not in client.get(path).text
    events = client.get("/api/events", params={"limit": 500}).text
    assert "task.integration_delivered" in events
    no_secret_anywhere(client, tmp_path, SECRET)
    assert SECRET not in caplog.text


def test_validation_connects_to_the_vetted_address_by_name(
    client: TestClient, upstream: Upstream
) -> None:
    item = mcp(client)
    act(client, item, "validate")
    sent = upstream.requests[-1]
    assert sent.url.host == "203.0.113.10"  # the address that was checked
    assert sent.headers["host"] == "mcp.example.test"
    assert sent.extensions["sni_hostname"] == "mcp.example.test"  # TLS checks the name


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        ("127.0.0.2", True),
        ("::ffff:127.0.0.1", True),
        ("[::1]", True),
        ("169.254.169.254", True),
        ("0.1.2.3", True),
        ("240.0.0.1", True),
        ("224.0.0.1", True),
        ("a.localhost", True),
        ("10.0.0.5", False),
        ("203.0.113.10", False),
        ("mcp.example.test", False),
    ],
)
def test_internal_addresses(host: str, internal: bool) -> None:
    from argus_api.integrations.checks import internal_address

    assert internal_address(host) is internal


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"[]", "invalid Kubernetes authorization response"),
        (b"{" + b" " * (70 * 1024) + b"}", "unreachable: ValueError"),
        (b"not json", "invalid Kubernetes authorization response"),
    ],
)
def test_kubernetes_answers_that_are_not_reviews_fail_closed(
    client: TestClient, body: bytes, detail: str
) -> None:
    item = k8s(client)
    app.state.integration_transport = httpx.MockTransport(
        lambda request: httpx.Response(201, content=body)
    )
    res = act(client, item, "validate").json()
    assert res["validation"] == "failed" and res["validation_detail"] == detail, res


def assert_redacted(*values: str) -> None:
    leaked = any(secret in value for secret in (SECRET, ROTATED) for value in values)
    assert not leaked, "credential present in public output"


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["newline", "crlf"])
def test_token_line_endings_are_rejected_before_network(
    client: TestClient, upstream: Upstream, ending: str
) -> None:
    item = mcp(client)
    for path, body in (
        (
            "/api/projects/alpha/integrations",
            {
                "kind": "mcp",
                "name": "other",
                "config": {"url": MCP_URL},
                "secret": {"token": SECRET + ending},
            },
        ),
        (
            f"/api/projects/alpha/integrations/{item['id']}/rotate",
            {"secret": {"token": ROTATED + ending}},
        ),
    ):
        res = client.post(path, json=body)
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "secret_invalid"
    assert client.portal is not None
    client.portal.call(
        app.state.secrets.put, f"integration-{item['id']}", {"token": SECRET + ending}
    )
    res = act(client, item, "validate")
    assert res.status_code == 422
    assert_redacted(res.text)
    assert not upstream.requests


@pytest.mark.parametrize("missing", [False, True], ids=["unavailable", "missing-ref"])
def test_store_failures_do_not_change_rows_or_audit(
    client: TestClient, upstream: Upstream, missing: bool
) -> None:
    item = mcp(client)
    name = f"integration-{item['id']}"
    if missing:
        assert client.portal is not None
        client.portal.call(app.state.secrets.delete, name)
    else:
        app.state.secrets_ok = False
    before = client.get("/api/events", params={"limit": 500}).json()
    for action, kw in (
        ("revoke", {}),
        ("rotate", {"json": {"secret": {"token": ROTATED}}}),
        ("validate", {}),
    ):
        res = act(client, item, action, **kw)
        assert res.status_code == 503
        assert res.json()["detail"]["code"] == "secret_store_unavailable"
        assert_redacted(res.text)
    if not missing:
        res = client.post(
            "/api/projects/alpha/integrations",
            json={
                "kind": "mcp",
                "name": "other",
                "config": {"url": MCP_URL},
                "secret": {"token": ROTATED},
            },
        )
        assert res.status_code == 503
        # Auth and project isolation retain their usual precedence.
        assert act(client, item, "revoke", project="beta").status_code == 404
        worker = register(client)
        assert act(client, item, "revoke", headers=worker).status_code == 403
    after = client.get(f"/api/projects/alpha/integrations/{item['id']}").json()
    assert after == item

    def integration_events(events):
        return [e for e in events if e["subject_type"] == "integration"]

    assert integration_events(
        client.get("/api/events", params={"limit": 500}).json()
    ) == integration_events(before)
    assert not upstream.requests
    assert (app.state.secrets.get(name) is None) is missing


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"status": {}},
        {"status": {"allowed": "false"}},
        {"status": {"allowed": False, "evaluationError": "indeterminate"}},
        {"status": {"allowed": True, "evaluationError": "indeterminate"}},
    ],
    ids=["empty", "empty-status", "non-bool", "error-denied", "error-allowed"],
)
def test_indeterminate_negative_kubernetes_probe_fails(
    client: TestClient, upstream: Upstream, reply: dict
) -> None:
    item = k8s(client)
    upstream.review_overrides["*:get::pods:"] = reply
    res = act(client, item, "validate").json()
    assert len(upstream.requests) == 2  # positive list allowed, then indeterminate
    assert res["validation"] == "failed"
    assert res["validation_detail"] == "invalid Kubernetes authorization response"


def rpc(result: dict, request_id: str = "argus-discover") -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


MODERN = {
    "resultType": "complete",
    "supportedVersions": ["2026-07-28"],
    "capabilities": {},
}
LEGACY = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "serverInfo": {"name": "fake", "version": "1"},
}


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize(
    "versions,protocol",
    [
        (["2026-07-28"], None),
        (["2025-11-25", "2026-07-28"], None),
        (["2025-11-25"], "2025-11-25"),
        (["2025-06-18"], "2025-06-18"),
        (["2025-03-26"], "2025-03-26"),
        (["2025-03-26", "2025-06-18", "2025-11-25"], "2025-11-25"),
        (["2025-03-26", "2025-06-18"], "2025-06-18"),
        (None, "2025-11-25"),
    ],
    ids=[
        "modern",
        "modern-preferred",
        "november",
        "june",
        "march",
        "newest-legacy",
        "june-preferred",
        "unknown-method",
    ],
)
def test_mcp_protocol_success(
    client: TestClient, sse: bool, versions: list[str] | None, protocol: str | None
) -> None:
    item = mcp(client)
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        methods.append(body["method"])
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        assert request.headers["accept"] == "application/json, text/event-stream"
        if body["method"] == "server/discover":
            assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
            assert request.headers["Mcp-Method"] == "server/discover"
            assert body["params"]["_meta"] == {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "argus", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            if versions is None:
                return httpx.Response(
                    400,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": "unknown method"},
                    },
                )
            reply = rpc({**MODERN, "supportedVersions": versions}, body["id"])
        else:
            assert body["method"] == "initialize"
            assert body["params"]["protocolVersion"] == protocol
            assert "Mcp-Method" not in request.headers
            assert "MCP-Protocol-Version" not in request.headers
            assert "_meta" not in body["params"]
            reply = rpc({**LEGACY, "protocolVersion": protocol}, body["id"])
        reply["echo"] = request.headers["authorization"]
        if sse:
            notification = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"data": SECRET},
                }
            )
            # SSE comments, CRLF and multiline data are valid.
            content = (
                ": keepalive\r\n\r\ndata: "
                + notification
                + "\r\n\r\nevent: message\r\ndata: "
                + json.dumps(reply, indent=2).replace("\n", "\r\ndata: ")
                + "\r\n\r\n"
            )
            return httpx.Response(
                200,
                content=content,
                headers={"Content-Type": "text/event-stream", "X-Echo": SECRET},
            )
        return httpx.Response(200, json=reply, headers={"X-Echo": SECRET})

    app.state.integration_transport = httpx.MockTransport(handler)
    result = act(client, item, "validate").json()
    assert result["validation"] == "ok"
    assert (
        result["validation_detail"]
        == "MCP response valid; credentials not explicitly rejected."
    )
    assert methods == (
        ["server/discover", "initialize"] if protocol else ["server/discover"]
    )


@pytest.mark.parametrize(
    "status,reply",
    [
        (401, {}),
        (403, {}),
        *[
            (
                400,
                {
                    "jsonrpc": "2.0",
                    "id": "argus-discover",
                    "error": {"code": code, "message": "error"},
                },
            )
            for code in (-32020, -32021, -32022)
        ],
        (
            200,
            {
                "jsonrpc": "2.0",
                "id": "argus-discover",
                "error": {"code": -32601, "message": "error"},
            },
        ),
        (200, rpc(MODERN, "wrong-id")),
        (200, {}),
        (200, []),
        (200, rpc({"supportedVersions": ["2026-07-28"], "capabilities": {}})),
        (200, rpc({**MODERN, "capabilities": []})),
        (200, rpc({**MODERN, "supportedVersions": []})),
        (200, rpc({**MODERN, "supportedVersions": ["2099-01-01"]})),
        (200, rpc({**MODERN, "supportedVersions": ["2025-11-25", 1]})),
        (200, rpc({**MODERN, "supportedVersions": ["2025-11-25"]}, "wrong-id")),
        (200, rpc({"supportedVersions": ["2025-11-25"], "capabilities": {}})),
        (200, rpc({**MODERN, "supportedVersions": ["2025-11-25"], "capabilities": []})),
    ],
    ids=[
        "401",
        "403",
        "modern-version-error",
        "modern-capability-error",
        "modern-header-error",
        "rpc-error-200",
        "wrong-id",
        "empty",
        "array",
        "no-complete",
        "bad-capabilities",
        "no-versions",
        "unsupported-versions",
        "legacy-invalid-version-list",
        "legacy-wrong-id",
        "legacy-no-complete",
        "legacy-bad-capabilities",
    ],
)
def test_mcp_invalid_response_never_succeeds_or_falls_back(
    client: TestClient, status: int, reply: Any
) -> None:
    item = mcp(client)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=reply, headers={"X-Echo": SECRET})

    app.state.integration_transport = httpx.MockTransport(handler)
    result = act(client, item, "validate").json()
    assert result["validation"] == "failed"
    assert len(requests) == 1
    assert_redacted(result["validation_detail"])


@pytest.mark.parametrize("reply", [b"not json", b"<html>ok</html>"])
def test_mcp_malformed_body(client: TestClient, reply: bytes) -> None:
    item = mcp(client)
    app.state.integration_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=reply)
    )
    assert act(client, item, "validate").json()["validation"] == "failed"


@pytest.mark.parametrize(
    "reply",
    [
        {},
        rpc(LEGACY, "wrong-id"),
        rpc({"protocolVersion": "2025-06-18"}, "argus-initialize"),
        rpc({**LEGACY, "protocolVersion": "2025-06-18"}, "argus-initialize"),
        {
            "jsonrpc": "2.0",
            "id": "argus-initialize",
            "error": {"code": -32601, "message": "error"},
        },
    ],
    ids=["empty", "wrong-id", "missing-fields", "wrong-version", "rpc-error"],
)
def test_legacy_2xx_is_not_enough(client: TestClient, reply: dict) -> None:
    item = mcp(client)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400)
        return httpx.Response(200, json=reply)

    app.state.integration_transport = httpx.MockTransport(handler)
    assert act(client, item, "validate").json()["validation"] == "failed"
    assert len(requests) == 2


async def test_core_debug_trace_cannot_log_credential_echo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from httpcore2 import AsyncMockBackend

    caplog.set_level(logging.DEBUG)
    # Exercise real HTTP/1.1 decoding and Trace, without sockets or DNS.
    payload = json.dumps(rpc(MODERN)).encode()
    raw = (
        b"HTTP/1.1 200 "
        + SECRET.encode()
        + b"\r\nX-Echo: "
        + SECRET.encode()
        + b"\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(payload)).encode()
        + b"\r\n\r\n"
        + payload
    )
    transport = httpx.AsyncHTTPTransport()
    transport._pool._network_backend = AsyncMockBackend([raw])
    result = await checks.check(
        "mcp", {"url": MCP_URL}, "read_only", SECRET, transport, _public
    )
    leaked = any(SECRET in record.getMessage() for record in caplog.records)
    caplog.clear()  # Never print a hostile header or dummy credential on failure.
    assert not leaked, "credential echoed into HTTP debug logging"
    assert result[0] == "ok"
    # Logging outside the check remains enabled.
    logging.getLogger("httpcore2.http11").debug("unrelated trace")
    assert "unrelated trace" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("action", ["create", "rotate", "revoke"])
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_cancellation_at_commit_keeps_secret_row_and_audit_coherent(
    db: Database, monkeypatch: pytest.MonkeyPatch, action: str, phase: str
) -> None:
    await insert_project(db, "alpha")
    store = MemorySecretStore()
    body = service.IntegrationCreate(
        kind="mcp",
        name="docs",
        config={"url": MCP_URL},
        secret=service.SecretIn(token=SECRET),
    )
    item = (
        None
        if action == "create"
        else await service.create(db, store, "alpha", body, "admin:test")
    )
    reached, release = asyncio.Event(), asyncio.Event()
    execute = db._writer._execute

    async def paused_commit(function, *args, **kwargs):
        sql = args[0] if args else None
        if sql == "COMMIT" and phase == "before":
            reached.set()
            await release.wait()
        result = await execute(function, *args, **kwargs)
        if sql == "COMMIT" and phase == "after":
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(db._writer, "_execute", paused_commit)
    if action == "create":
        operation = service.create(db, store, "alpha", body, "admin:test")
    elif action == "rotate":
        assert item is not None
        operation = service.rotate(
            db,
            store,
            "alpha",
            item.id,
            service.IntegrationRotate(secret=service.SecretIn(token=ROTATED)),
            "admin:test",
        )
    else:
        assert item is not None
        operation = service.revoke(db, store, "alpha", item.id, "admin:test")
    published = []
    publish = db.bus.publish

    def capture(event):
        published.append(event.kind)
        publish(event)

    monkeypatch.setattr(db.bus, "publish", capture)
    task = asyncio.create_task(operation)
    milestone = asyncio.create_task(reached.wait())
    done, _ = await asyncio.wait((task, milestone), return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        # Release the hook even on an unexpected early return; propagate its
        # error directly instead of leaving an event waiter behind.
        reached.set()
        release.set()
        await milestone
        await task
        pytest.fail("mutation completed before the commit milestone")
    await milestone
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    rows = await service.list_all(db, "alpha")
    # Cancellation is deferred: the mutation finalizes, including audit publication.
    assert len(rows) == 1
    row = rows[0]
    assert row.state == ("revoked" if action == "revoke" else "active")
    assert row.has_secret is (action != "revoke")
    stored = store.get(f"integration-{row.id}")
    coherent = (
        (stored is None)
        if action == "revoke"
        else bool(
            stored and stored["token"] == (ROTATED if action == "rotate" else SECRET)
        )
    )
    assert coherent, "credential state does not match the committed row"
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT kind FROM events WHERE subject_type = 'integration' ORDER BY id"
        ) as cur,
    ):
        kinds = [r[0] for r in await cur.fetchall()]
    expected = ["integration.created"]
    if action != "create":
        expected.append(f"integration.{'rotated' if action == 'rotate' else 'revoked'}")
    assert kinds == expected
    assert published == expected[-1:]


@pytest.mark.parametrize(
    "failure", ["get", "delete-noop"], ids=["unreadable", "unconfirmed-delete"]
)
def test_revoke_requires_readable_credential_and_confirmed_deletion(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    item = mcp(client)
    store = app.state.secrets
    if failure == "get":

        def unreadable(name):
            raise SecretStoreError(SECRET)

        monkeypatch.setattr(store, "get", unreadable)
    else:

        async def noop(name):
            pass

        monkeypatch.setattr(store, "delete", noop)
    res = act(client, item, "revoke")
    assert res.status_code == 503
    assert_redacted(res.text)
    assert client.get(f"/api/projects/alpha/integrations/{item['id']}").json() == item
    events = client.get("/api/events", params={"limit": 500}).json()
    assert not any(e["kind"] == "integration.revoked" for e in events)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("action", ["create", "rotate", "revoke"])
@pytest.mark.parametrize("committed", [False, True], ids=["rollback", "committed"])
async def test_commit_error_compensates_only_confirmed_rollback(
    db: Database, monkeypatch: pytest.MonkeyPatch, action: str, committed: bool
) -> None:
    await insert_project(db, "alpha")
    store = MemorySecretStore()
    body = service.IntegrationCreate(
        kind="mcp",
        name="docs",
        config={"url": MCP_URL},
        secret=service.SecretIn(token=SECRET),
    )
    item = (
        None
        if action == "create"
        else await service.create(db, store, "alpha", body, "admin:test")
    )
    execute = db._writer._execute
    injected = False

    async def interrupted(function, *args, **kwargs):
        nonlocal injected
        if args and args[0] == "COMMIT" and not injected:
            injected = True
            if committed:
                await execute(function, *args, **kwargs)
            raise asyncio.CancelledError
        return await execute(function, *args, **kwargs)

    monkeypatch.setattr(db._writer, "_execute", interrupted)
    with pytest.raises(asyncio.CancelledError):
        if action == "create":
            await service.create(db, store, "alpha", body, "admin:test")
        elif action == "rotate":
            assert item is not None
            await service.rotate(
                db,
                store,
                "alpha",
                item.id,
                service.IntegrationRotate(secret=service.SecretIn(token=ROTATED)),
                "admin:test",
            )
        else:
            assert item is not None
            await service.revoke(db, store, "alpha", item.id, "admin:test")
    monkeypatch.undo()
    rows = await service.list_all(db, "alpha")
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT kind FROM events WHERE subject_type = 'integration' ORDER BY id"
        ) as cur,
    ):
        kinds = [r[0] for r in await cur.fetchall()]
    if action == "create" and not committed:
        assert not rows and not store.names() and not kinds
        return
    row = rows[0]
    revoked = committed and action == "revoke"
    assert row.state == ("revoked" if revoked else "active")
    assert row.has_secret is (not revoked)
    stored = store.get(f"integration-{row.id}")
    if revoked:
        assert stored is None
    else:
        expected_token = ROTATED if committed and action == "rotate" else SECRET
        matches = bool(stored and stored["token"] == expected_token)
        assert matches, "credential does not match the confirmed database outcome"
    assert kinds == (
        [
            "integration.created",
            f"integration.{'rotated' if action == 'rotate' else 'revoked'}",
        ]
        if committed and action != "create"
        else ["integration.created"]
    )


@pytest.mark.parametrize("code", [-32020, -32021, -32022])
def test_modern_sse_errors_do_not_downgrade(client: TestClient, code: int) -> None:
    item = mcp(client)
    requests = []

    def handler(request):
        requests.append(request)
        body = {"jsonrpc": "2.0", "error": {"code": code, "message": SECRET}}
        return httpx.Response(
            400,
            content="data: " + json.dumps(body) + "\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    app.state.integration_transport = httpx.MockTransport(handler)
    result = act(client, item, "validate").json()
    assert result["validation"] == "failed" and len(requests) == 1
    assert_redacted(result["validation_detail"])


async def test_mcp_sse_stops_at_final_response_without_waiting_for_eof() -> None:
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b": keepalive\r\n\r\n"
            payload = json.dumps(rpc(MODERN)).encode()
            yield b"data: " + payload[:30]
            yield payload[30:] + b"\r\n\r\n"
            pytest.fail("read beyond the final SSE response")

        async def aclose(self):
            self.closed = True

    stream = Stream()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, stream=stream, headers={"Content-Type": "text/event-stream"}
        )
    )
    result = await checks.check(
        "mcp", {"url": MCP_URL}, "read_only", SECRET, transport, _public
    )
    assert result == ("ok", checks.MCP_SUCCESS)
    assert stream.closed


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("action", ["create", "rotate", "revoke"])
async def test_cancellation_finishes_sops_edit_before_database_outcome(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    await insert_project(db, "alpha")
    path = tmp_path / "fake.sops.yaml"
    path.write_text("fake encrypted file")
    store = SopsSecretStore(path)
    durable: dict[str, dict] = {}
    reached, release = asyncio.Event(), asyncio.Event()
    pause = False

    async def fake_edit(args, name, stdin):
        # Model rename completing before the store's memory cache is updated.
        if args[0] == "set":
            durable[name] = json.loads(stdin)
        else:
            durable.pop(name, None)
        if pause:
            reached.set()
            await release.wait()

    async def never_run(*args, **kwargs):
        pytest.fail("fake SOPS test attempted to start a subprocess")

    monkeypatch.setattr(store, "_edit", fake_edit)
    monkeypatch.setattr(store, "_run", never_run)
    body = service.IntegrationCreate(
        kind="mcp",
        name="docs",
        config={"url": MCP_URL},
        secret=service.SecretIn(token=SECRET),
    )
    item = (
        None
        if action == "create"
        else await service.create(db, store, "alpha", body, "admin:test")
    )
    pause = True
    if action == "create":
        operation = service.create(db, store, "alpha", body, "admin:test")
    elif action == "rotate":
        assert item is not None
        operation = service.rotate(
            db,
            store,
            "alpha",
            item.id,
            service.IntegrationRotate(secret=service.SecretIn(token=ROTATED)),
            "admin:test",
        )
    else:
        assert item is not None
        operation = service.revoke(db, store, "alpha", item.id, "admin:test")
    task = asyncio.create_task(operation)
    milestone = asyncio.create_task(reached.wait())
    done, _ = await asyncio.wait((task, milestone), return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        reached.set()
        release.set()
        await milestone
        await task
        pytest.fail("mutation completed before SOPS edit milestone")
    await milestone
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await service.list_all(db, "alpha")
    assert len(rows) == 1
    row = rows[0]
    assert row.state == ("revoked" if action == "revoke" else "active")
    name = f"integration-{row.id}"
    coherent = store.get(name) == durable.get(name)
    assert coherent, "SOPS cache and completed edit disagree"
    if action == "revoke":
        assert not durable and store.get(name) is None
    else:
        expected = ROTATED if action == "rotate" else SECRET
        coherent = bool(durable.get(name) and durable[name]["token"] == expected)
        assert coherent, "SOPS edit does not match the committed row"
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT kind FROM events WHERE subject_id = ? ORDER BY id", (row.id,)
        ) as cur,
    ):
        kinds = [r[0] for r in await cur.fetchall()]
    assert kinds == (
        ["integration.created"]
        if action == "create"
        else [
            "integration.created",
            f"integration.{'rotated' if action == 'rotate' else 'revoked'}",
        ]
    )
