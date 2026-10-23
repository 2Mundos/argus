"""REST and MCP surface of the project runtime declaration (authorization,
validation, propagation). Preparation itself is in test_project_runtime.py."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, register, rpc, tool

BODY = {
    "components": [{"id": "psql"}, {"id": "postgres-server"}, {"id": "valkey"}, {"id": "minio", "required": False}],
    "services": [{"kind": "postgres", "env": {"RADAR_TEST_DATABASE_URL": "{url_asyncpg}"}}],
}  # fmt: skip


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv(
        "ARGUS_PROJECT_RUNTIME", "off"
    )  # no builder loop, no Docker, in API tests
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        for slug in ("radar", "argus"):
            assert (
                c.put(f"/api/projects/{slug}", json={"name": slug}).status_code == 200
            )
        yield c


def as_agent(client: TestClient, role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {register(client, role)}"}


def test_admin_declares_and_everyone_reads_the_effective_view(
    client: TestClient,
) -> None:
    res = client.put("/api/projects/radar/runtime", json=BODY)
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["declared"] and [g["component"] for g in view["gaps"]] == ["minio"]
    assert {c["id"] for c in view["requirement"]} == {
        "psql",
        "postgres-server",
        "valkey",
    }
    assert client.get("/api/projects/argus/runtime").json()["declared"] is False


def test_only_admins_may_declare(client: TestClient) -> None:
    for role in ("worker", "orchestrator"):
        res = client.put(
            "/api/projects/radar/runtime", json=BODY, headers=as_agent(client, role)
        )
        assert res.status_code == 403, role
    assert (
        client.put(
            "/api/projects/radar/runtime", json=BODY, headers={"Authorization": ""}
        ).status_code
        == 401
    )


def test_unapproved_declaration_is_422_with_every_problem(client: TestClient) -> None:
    bad = {
        "components": [{"id": "psql", "version": "9"}, {"id": "curl-bash"}],
        "services": [],
    }
    res = client.put("/api/projects/radar/runtime", json=bad)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "runtime_invalid"
    assert len(res.json()["detail"]["details"]) == 2


def test_arbitrary_build_instructions_cannot_be_smuggled_in(client: TestClient) -> None:
    for extra in (
        {"dockerfile": "FROM evil"},
        {"build_args": {"A": "b"}},
        {"env": {"A": "b"}},
    ):
        res = client.put(
            "/api/projects/radar/runtime", json={"components": [], **extra}
        )
        assert res.status_code == 422, extra
    res = client.put(
        "/api/projects/radar/runtime",
        json={"components": [{"id": "psql", "run": "curl x | sh"}]},
    )
    assert res.status_code == 422


def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.put("/api/projects/nope/runtime", json=BODY).status_code == 404


def test_worker_reads_only_projects_it_leased(client: TestClient) -> None:
    worker = as_agent(client, "worker")
    assert client.get("/api/projects/radar/runtime", headers=worker).status_code == 403
    client.post("/api/tasks", json={"title": "t", "project": "radar"})
    assert client.post("/api/tasks/claim", json={}, headers=worker).status_code == 200
    assert client.get("/api/projects/radar/runtime", headers=worker).status_code == 200
    assert client.get("/api/projects/argus/runtime", headers=worker).status_code == 403


def test_prepare_needs_an_agent_and_reports_a_disabled_runtime(
    client: TestClient,
) -> None:
    client.put("/api/projects/radar/runtime", json=BODY)
    client.post("/api/tasks", json={"title": "t", "project": "radar"})
    worker = as_agent(client, "worker")
    task = client.post("/api/tasks/claim", json={}, headers=worker).json()
    body = {
        "task_id": task["id"], "attempt": task["attempt"], "host_id": "d", "run_as": "1000:1000",
        "base_ref": "img:1", "base_id": "sha256:" + "a" * 64, "platform": "linux/amd64",
    }  # fmt: skip
    assert (
        client.post("/api/runtime/prepare", json=body).status_code == 403
    )  # admin is no agent
    res = client.post("/api/runtime/prepare", json=body, headers=worker)
    assert res.status_code == 200 and res.json()["state"] == "disabled"
    assert (
        client.get(f"/api/tasks/{task['id']}").json()["state"] == "claimed"
    )  # untouched


def test_prepare_rejects_malformed_identity_fields(client: TestClient) -> None:
    worker = as_agent(client, "worker")
    for patch in (
        {"run_as": "root"},
        {"base_id": "latest"},
        {"platform": "amd64"},
        {"base_ref": "a b; rm"},
    ):
        body = {"task_id": "t", "attempt": 1, "host_id": "d", "run_as": "1000:1000",
                "base_ref": "img:1", "base_id": "sha256:" + "a" * 64, "platform": "linux/amd64", **patch}  # fmt: skip
        assert (
            client.post("/api/runtime/prepare", json=body, headers=worker).status_code
            == 422
        ), patch


def test_catalog_lists_pins_and_gaps(client: TestClient) -> None:
    catalog = {c["id"]: c for c in client.get("/api/runtime/catalog").json()}
    assert catalog["psql"]["versions"] == ["16"] and catalog["valkey"]["versions"] == [
        "8.0"
    ]
    assert catalog["minio"]["kind"] == "gap"


def test_mcp_declares_reads_and_is_listed_by_scope(client: TestClient) -> None:
    worker = register(client)
    assert "get_project_runtime" in {
        t["name"] for t in rpc(client, worker, "tools/list")["tools"]
    }
    assert "set_project_runtime" not in {
        t["name"] for t in rpc(client, worker, "tools/list")["tools"]
    }
    set_ = tool(
        client,
        ADMIN_TOKEN,
        "set_project_runtime",
        project="radar",
        components=BODY["components"],
        services=BODY["services"],
    )
    assert set_["declared"] and set_["services"][0]["kind"] == "postgres"
    assert tool(client, ADMIN_TOKEN, "get_project_runtime", project="radar") == set_
    bad = tool(
        client,
        ADMIN_TOKEN,
        "set_project_runtime",
        project="radar",
        components=[{"id": "x"}],
    )
    assert "unknown component" in str(bad)
    assert (
        "requires scope"
        in tool(client, worker, "set_project_runtime", project="radar", components=[])[
            "error"
        ]
    )
    rebuilt = tool(client, ADMIN_TOKEN, "rebuild_project_runtime", project="radar")
    assert rebuilt["builds"] == []
