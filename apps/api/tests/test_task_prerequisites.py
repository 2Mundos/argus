"""Prerequisite evidence view (docs/specs/task-discovery.spec.md).

Forge is a mock httpx transport; no live forge."""

from collections.abc import Generator
from pathlib import Path

import httpx2 as httpx
import pytest
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN as MCP_ADMIN
from test_mcp import tool

HOST, OWNER, REPO = "git.example.test", "team", "argus"
URL = f"https://{HOST}/{OWNER}/{REPO}/pulls/12"
ADMIN_TOKEN = MCP_ADMIN


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def forge(client: TestClient, handler) -> None:
    client.app.state.pr_checker.transport = httpx.MockTransport(handler)  # ty: ignore[unresolved-attribute]


def setup_repository(client: TestClient) -> None:
    store = MemorySecretStore()
    client.app.state.secrets = store  # ty: ignore[unresolved-attribute]
    client.app.state.pr_checker.secrets = store  # ty: ignore[unresolved-attribute]
    res = client.put(
        "/api/pools/web",
        json={
            "runtime": "nanoclaw",
            "image": "h",
            "max_replicas": 1,
            "docker": {"runtime": "runsc"},
            "nanoclaw": {
                "install_dir": "/opt/nanoclaw",
                "instances_root": "/srv/i",
                "agent_image": "a:1",
                "repositories": {"global": f"https://{HOST}/{OWNER}/{REPO}"},
            },
        },
    )
    assert res.status_code in (200, 201), res.text
    res = client.post(
        "/api/accounts",
        json={
            "name": "repo",
            "provider": "gitea",
            "credential": {"username": "bot", "token": "dummy-token"},
        },
    )
    assert res.status_code == 201, res.text


def worker_headers(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    token = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def succeed(
    client: TestClient,
    worker: dict[str, str],
    *,
    title: str = "dep",
    summary: str = "done",
    artifacts: list[str] | None = None,
) -> dict:
    tid = client.post("/api/tasks", json={"project": "global", "title": title}).json()[
        "id"
    ]
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == tid
    body: dict = {"attempt": claimed["attempt"], "result": {"summary": summary}}
    if artifacts is not None:
        body["result"]["artifacts"] = artifacts
    res = client.post(f"/api/tasks/{tid}/complete", json=body, headers=worker)
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "succeeded"
    return res.json()


def dependent(client: TestClient, dep_ids: list[str]) -> dict:
    res = client.post(
        "/api/tasks",
        json={"project": "global", "title": "child", "depends_on": dep_ids},
    )
    assert res.status_code == 201, res.text
    return res.json()


def pr_body(**extra):
    base = {
        "state": "closed",
        "merged": True,
        "merged_at": "2026-10-24T09:00:00Z",
        "base": {"ref": "main"},
        "head": {"sha": "abc1234"},
    }
    base.update(extra)
    return base


def test_succeeded_with_closed_unmerged_pr_is_unsatisfied(client: TestClient) -> None:
    setup_repository(client)
    forge(
        client,
        lambda r: httpx.Response(
            200, json={"state": "closed", "merged": False, "base": {"ref": "main"}}
        ),
    )
    worker = worker_headers(client)
    dep = succeed(client, worker, summary=f"see {URL}")
    child = dependent(client, [dep["id"]])
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    assert got["enforced"] is False
    [item] = got["items"]
    assert item["task_succeeded"] is True
    assert item["verdict"] == "unsatisfied"
    assert item["why"] == "closed_without_merge"
    assert item["evidence"]["pr_state"] == "closed"
    assert item["task_succeeded"] is not (item["verdict"] == "satisfied")


def test_merged_pr_is_satisfied(client: TestClient) -> None:
    setup_repository(client)
    forge(client, lambda r: httpx.Response(200, json=pr_body()))
    worker = worker_headers(client)
    dep = succeed(client, worker, summary=f"Delivered in {URL}")
    child = dependent(client, [dep["id"]])
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["verdict"] == "satisfied"
    assert item["why"] == "merged"
    assert item["evidence"]["base_ref"] == "main"
    assert item["evidence"]["head_sha"] == "abc1234"
    assert "review_state_is_not_delivery_proof" in item["evidence"]["notes"]


def test_base_mismatch_with_expected_base(client: TestClient) -> None:
    setup_repository(client)
    forge(client, lambda r: httpx.Response(200, json=pr_body(base={"ref": "develop"})))
    worker = worker_headers(client)
    dep = succeed(client, worker, summary=f"Delivered in {URL}")
    child = dependent(client, [dep["id"]])
    got = client.get(
        f"/api/tasks/{child['id']}/prerequisites",
        params={"expected_base": "main"},
    ).json()
    [item] = got["items"]
    assert item["verdict"] == "unsatisfied"
    assert item["why"] == "base_mismatch"


def test_merged_with_pending_reviews_stays_satisfied(client: TestClient) -> None:
    setup_repository(client)
    forge(client, lambda r: httpx.Response(200, json=pr_body()))
    worker = worker_headers(client)
    dep = succeed(client, worker, summary=f"Delivered in {URL}")
    child = dependent(client, [dep["id"]])
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["verdict"] == "satisfied"
    assert "review_state_is_not_delivery_proof" in item["evidence"]["notes"]


def test_forge_unavailable_missing_credential(client: TestClient) -> None:
    setup_repository(client)
    worker = worker_headers(client)
    dep = succeed(client, worker, summary=f"Delivered in {URL}")
    child = dependent(client, [dep["id"]])
    client.app.state.pr_checker.secrets = MemorySecretStore()  # ty: ignore[unresolved-attribute]
    client.app.state.pr_checker._cache.clear()  # ty: ignore[unresolved-attribute]
    forge(client, lambda r: httpx.Response(200, json=pr_body()))
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["verdict"] == "unknown"
    assert item["why"] == "forge_unavailable"
    assert "forge" in got["partial"]


def test_forge_unavailable_http_error(client: TestClient) -> None:
    setup_repository(client)
    worker = worker_headers(client)
    dep = succeed(client, worker, summary=f"Delivered in {URL}")
    child = dependent(client, [dep["id"]])
    forge(client, lambda r: httpx.Response(500, json={}))
    client.app.state.pr_checker._cache.clear()  # ty: ignore[unresolved-attribute]
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["verdict"] == "unknown"
    assert item["why"] == "forge_unavailable"
    assert "forge" in got["partial"]


def test_artifact_verified_and_missing_corrupt(client: TestClient) -> None:
    from argus_api.artifacts.service import key as artifact_key

    worker = worker_headers(client)
    tid = client.post("/api/tasks", json={"project": "global", "title": "art"}).json()[
        "id"
    ]
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    up = client.post(
        f"/api/tasks/{tid}/artifacts",
        params={"attempt": claimed["attempt"], "name": "proof.txt"},
        content=b"proof-bytes",
        headers={**worker, "Content-Type": "text/plain"},
    )
    assert up.status_code == 201, up.text
    art = up.json()
    res = client.post(
        f"/api/tasks/{tid}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "no pr", "artifacts": [art["id"]]},
        },
        headers=worker,
    )
    assert res.status_code == 200, res.text
    dep = res.json()
    child = dependent(client, [dep["id"]])
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["verdict"] == "satisfied"
    assert item["why"] == "artifact_verified"
    assert item["evidence"]["artifacts"][0]["verified"] is True

    # corrupt on disk
    store = client.app.state.artifacts  # ty: ignore[unresolved-attribute]
    path = store.path(artifact_key(tid, claimed["attempt"], "proof.txt"))
    path.write_bytes(b"tampered")
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["verdict"] == "unsatisfied"
    assert item["why"] == "artifact_corrupted"

    path.unlink()
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["verdict"] == "unsatisfied"
    assert item["why"] == "artifact_missing"


def test_no_reference_is_unknown(client: TestClient) -> None:
    worker = worker_headers(client)
    dep = succeed(client, worker, summary="nothing to see")
    child = dependent(client, [dep["id"]])
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    [item] = got["items"]
    assert item["task_succeeded"] is True
    assert item["verdict"] == "unknown"
    assert item["why"] == "no_reference"


def test_over_cap_dependencies_omitted(client: TestClient) -> None:
    worker = worker_headers(client)
    deps = [succeed(client, worker, title=f"d{n}")["id"] for n in range(26)]
    child = dependent(client, deps)
    got = client.get(f"/api/tasks/{child['id']}/prerequisites").json()
    assert len(got["items"]) == 25
    assert got["omitted"] == ["dependencies"]
    assert "dependencies" in got["partial"]


def test_worker_not_leased_and_unknown_task(client: TestClient) -> None:
    worker = worker_headers(client)
    dep = succeed(client, worker, summary="x")
    child = dependent(client, [dep["id"]])
    outsider = worker_headers(client)
    res = client.get(f"/api/tasks/{child['id']}/prerequisites", headers=outsider)
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "not_leased"
    res = client.get("/api/tasks/no-such-task/prerequisites")
    assert res.status_code == 404


def test_mcp_get_task_prerequisites(client: TestClient) -> None:
    setup_repository(client)
    forge(client, lambda r: httpx.Response(200, json=pr_body()))
    worker = worker_headers(client)
    dep = succeed(client, worker, summary=f"Delivered in {URL}")
    child = dependent(client, [dep["id"]])
    got = tool(client, ADMIN_TOKEN, "get_task_prerequisites", task_id=child["id"])
    assert got["enforced"] is False
    assert got["items"][0]["verdict"] == "satisfied"
    got = tool(
        client,
        ADMIN_TOKEN,
        "get_task_prerequisites",
        task_id=child["id"],
        expected_base="other",
    )
    assert got["items"][0]["why"] == "base_mismatch"
