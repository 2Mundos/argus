"""Single-task read carries quiet facts (stall-visibility.spec.md 3.4)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api import reaper
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "x" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
OBS = {
    "workspace": "missing",
    "session": "running",
    "delegate": "silent",
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers=ADMIN) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


def claim(client: TestClient, worker: dict[str, str], title: str) -> dict:
    res = client.post("/api/tasks", json={"project": "global", "title": title})
    assert res.status_code == 201
    res = client.post("/api/tasks/claim", json={}, headers=worker)
    assert res.status_code == 200, res.text
    return res.json()


def heartbeat(client: TestClient, worker: dict[str, str], task: dict) -> None:
    res = client.post(
        "/api/agents/me/heartbeat",
        json={
            "leases": [
                {
                    "task_id": task["id"],
                    "attempt": task["attempt"],
                    "observation": OBS,
                }
            ]
        },
        headers=worker,
    )
    assert res.status_code == 200, res.text


async def run_reaper(db) -> None:
    async with db.write() as tx:
        await reaper.reap(tx, quiet_claim_s=0, stall_fail_s=0)


def test_task_read_and_inbox_share_quiet_facts(client: TestClient) -> None:
    worker = register(client)
    quiet = claim(client, worker, "quiet-one")
    heartbeat(client, worker, quiet)

    client.portal.call(run_reaper, app.state.db)  # type: ignore[union-attr]

    busy = claim(client, worker, "busy-one")

    task_res = client.get(f"/api/tasks/{quiet['id']}")
    assert task_res.status_code == 200
    body = task_res.json()
    assert body["quiet_at"] is not None
    assert body["stall_cause"] == "workspace_missing"
    assert body["last_signal_at"] is not None
    assert body["last_signal_source"] == "claim"

    inbox_res = client.get("/api/inbox?view=detail")
    assert inbox_res.status_code == 200
    row = next(i for i in inbox_res.json()["items"] if i["subject_id"] == quiet["id"])
    assert row["stall_cause"] == body["stall_cause"]
    assert row["last_signal_at"] == body["last_signal_at"]
    assert row["last_signal_source"] == body["last_signal_source"]

    busy_res = client.get(f"/api/tasks/{busy['id']}").json()
    assert busy_res["quiet_at"] is None
    assert busy_res["stall_cause"] is None
    assert busy_res["last_signal_at"] is not None
    assert busy_res["last_signal_source"] == "claim"

    keys = {"quiet_at", "last_signal_at", "last_signal_source", "stall_cause"}
    listed = client.get("/api/tasks").json()["items"]
    quiet_summary = next(i for i in listed if i["id"] == quiet["id"])
    assert not keys & set(quiet_summary)

    detail = client.get(f"/api/tasks?ids={quiet['id']}&view=detail").json()["items"][0]
    assert not keys & set(detail)


def test_released_quiet_task_has_no_quiet_facts(client: TestClient) -> None:
    worker = register(client)
    quiet = claim(client, worker, "quiet-release")
    heartbeat(client, worker, quiet)
    client.portal.call(run_reaper, app.state.db)  # type: ignore[union-attr]

    res = client.post(f"/api/tasks/{quiet['id']}/release", json={"reason": "test"})
    assert res.status_code == 200, res.text

    task_res = client.get(f"/api/tasks/{quiet['id']}")
    assert task_res.status_code == 200
    body = task_res.json()
    assert body["state"] == "ready"
    assert body["quiet_at"] is None
    assert body["stall_cause"] is None
    assert body["last_signal_at"] is None
    assert body["last_signal_source"] is None

    inbox_res = client.get("/api/inbox?view=detail")
    assert inbox_res.status_code == 200
    assert not any(i["subject_id"] == quiet["id"] for i in inbox_res.json()["items"])
