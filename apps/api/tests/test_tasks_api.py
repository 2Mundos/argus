import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

AGENT = {"X-Argus-Agent": "worker-1"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def test_create_get_list(client: TestClient) -> None:
    res = client.post("/tasks", json={"title": "write docs", "priority": 2})
    assert res.status_code == 201
    task = res.json()
    assert task["state"] == "ready"
    assert task["created_by"] == "human:anonymous"
    assert client.get(f"/tasks/{task['id']}").json()["title"] == "write docs"
    assert [t["id"] for t in client.get("/tasks?state=ready").json()] == [task["id"]]
    assert client.get("/tasks?state=failed").json() == []


def test_claim_and_complete(client: TestClient) -> None:
    task = client.post("/tasks", json={"title": "x"}).json()
    claimed = client.post("/tasks/claim", json={}, headers=AGENT).json()
    assert claimed["id"] == task["id"]
    res = client.post(
        f"/tasks/{task['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "done"}},
        headers=AGENT,
    )
    assert res.status_code == 200
    assert res.json()["state"] == "succeeded"


def test_claim_with_nothing_ready_returns_204(client: TestClient) -> None:
    res = client.post("/tasks/claim", json={"wait_s": 0}, headers=AGENT)
    assert res.status_code == 204


def test_claim_long_poll_wakes_when_a_task_becomes_ready(client: TestClient) -> None:
    with ThreadPoolExecutor(1) as pool:
        started = time.monotonic()
        waiting = pool.submit(
            client.post, "/tasks/claim", json={"wait_s": 10}, headers=AGENT
        )
        time.sleep(0.3)
        task = client.post("/tasks", json={"title": "late"}).json()
        res = waiting.result(timeout=5)
    assert res.status_code == 200
    assert res.json()["id"] == task["id"]
    assert time.monotonic() - started < 5


def test_worker_operations_require_an_agent(client: TestClient) -> None:
    assert client.post("/tasks/claim", json={}).status_code == 401


def test_errors_have_actionable_codes(client: TestClient) -> None:
    task = client.post("/tasks", json={"title": "x"}).json()
    client.post("/tasks/claim", json={}, headers=AGENT)
    res = client.post(
        f"/tasks/{task['id']}/complete", json={"attempt": 7}, headers=AGENT
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "stale_attempt"

    res = client.post(f"/tasks/{task['id']}/unblock")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "invalid_transition"

    res = client.get("/tasks/missing")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "not_found"
