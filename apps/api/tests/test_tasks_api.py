import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/agents/bootstrap-tokens", json={}).json()["token"]
    res = client.post("/agents/register", json={"bootstrap_token": boot})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return register(client)


def test_create_get_list(client: TestClient) -> None:
    res = client.post("/tasks", json={"title": "write docs", "priority": 2})
    assert res.status_code == 201
    task = res.json()
    assert task["state"] == "ready"
    assert task["created_by"] == "human:anonymous"
    assert client.get(f"/tasks/{task['id']}").json()["title"] == "write docs"
    assert [t["id"] for t in client.get("/tasks?state=ready").json()] == [task["id"]]
    assert client.get("/tasks?state=failed").json() == []


def test_claim_and_complete(client: TestClient, agent: dict[str, str]) -> None:
    task = client.post("/tasks", json={"title": "x"}).json()
    claimed = client.post("/tasks/claim", json={}, headers=agent).json()
    assert claimed["id"] == task["id"]
    res = client.post(
        f"/tasks/{task['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "done"}},
        headers=agent,
    )
    assert res.status_code == 200
    assert res.json()["state"] == "succeeded"


def test_claim_with_nothing_ready_returns_204(
    client: TestClient, agent: dict[str, str]
) -> None:
    res = client.post("/tasks/claim", json={"wait_s": 0}, headers=agent)
    assert res.status_code == 204


def test_claim_long_poll_wakes_when_a_task_becomes_ready(
    client: TestClient, agent: dict[str, str]
) -> None:
    with ThreadPoolExecutor(1) as pool:
        started = time.monotonic()
        waiting = pool.submit(
            client.post, "/tasks/claim", json={"wait_s": 10}, headers=agent
        )
        time.sleep(0.3)
        task = client.post("/tasks", json={"title": "late"}).json()
        res = waiting.result(timeout=5)
    assert res.status_code == 200
    assert res.json()["id"] == task["id"]
    assert time.monotonic() - started < 5


def test_worker_operations_require_an_agent(client: TestClient) -> None:
    assert client.post("/tasks/claim", json={}).status_code == 401


def test_invalid_token_is_401_not_anonymous(client: TestClient) -> None:
    res = client.post(
        "/tasks", json={"title": "x"}, headers={"Authorization": "Bearer nope"}
    )
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "invalid_token"


def test_agent_lifecycle_over_http(client: TestClient, agent: dict[str, str]) -> None:
    task = client.post("/tasks", json={"title": "x"}).json()
    claimed = client.post("/tasks/claim", json={}, headers=agent).json()
    hb = client.post(
        "/agents/me/heartbeat",
        json={"leases": [{"task_id": task["id"], "attempt": claimed["attempt"]}]},
        headers=agent,
    ).json()
    assert [r["task_id"] for r in hb["renewed"]] == [task["id"]]
    assert hb["heartbeat_interval_s"] == 30
    [listed] = client.get("/agents").json()
    assert listed["claimed_tasks"] == 1

    left = client.post("/agents/me/leave", headers=agent).json()
    assert left["state"] == "gone"
    assert client.get(f"/tasks/{task['id']}").json()["state"] == "ready"
    assert (
        client.post("/agents/me/heartbeat", json={}, headers=agent).status_code == 401
    )


def test_errors_have_actionable_codes(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = client.post("/tasks", json={"title": "x"}).json()
    client.post("/tasks/claim", json={}, headers=agent)
    res = client.post(
        f"/tasks/{task['id']}/complete", json={"attempt": 7}, headers=agent
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "stale_attempt"

    res = client.post(f"/tasks/{task['id']}/unblock")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "invalid_transition"

    res = client.get("/tasks/missing")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "not_found"
