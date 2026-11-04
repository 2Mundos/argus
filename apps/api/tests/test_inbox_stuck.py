"""Stuck-block flag on inbox rows (MCP operations 3.2): derived, read-only."""

import sqlite3
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import pytest
from argus_api.db import ts, utcnow
from argus_api.main import app
from fastapi.testclient import TestClient
from test_list_reads import create, ok, register

ADMIN_TOKEN = "test-admin-token-" + "s" * 32
KEY = "inbox.stuck_after_s"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        c.put("/api/projects/alpha", json={"name": "alpha"})
        yield c


def blocked_for(client: TestClient, data_dir: Path, hours: float) -> str:
    """A task blocked as worker_env, its block backdated by `hours`."""
    worker = register(client)
    task = create(client)
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    res = client.post(
        f"/api/tasks/{task['id']}/block",
        json={"attempt": claimed["attempt"], "reason": "worker_env"},
        headers=worker,
    )
    assert res.status_code == 200, res.text
    at = ts(utcnow() - timedelta(hours=hours))
    with sqlite3.connect(data_dir / "argus.db") as conn:
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (at, task["id"]))
    return task["id"]


def stuck_of(client: TestClient, task_id: str, **params) -> dict | None:
    items = ok(client.get("/api/inbox", params=params))["items"]
    [row] = [i for i in items if i["subject_id"] == task_id]
    return row["stuck"]


def test_default_threshold_flags_old_blocks_only(
    client: TestClient, tmp_path: Path
) -> None:
    [definition] = [
        d for d in ok(client.get("/api/settings/registry")) if d["key"] == KEY
    ]
    assert definition["default"] == 21600
    old, fresh = blocked_for(client, tmp_path, 7), blocked_for(client, tmp_path, 5)
    stuck = stuck_of(client, old)
    assert stuck["kind"] == "block_reason" and stuck["reason"] == "worker_env"
    assert stuck["threshold_s"] == 21600
    assert 7 * 3600 <= stuck["age_s"] < 7 * 3600 + 300
    assert stuck_of(client, fresh) is None
    # the actionable view carries the same flag
    page = ok(client.get("/api/inbox", params={"view": "actionable"}))
    [row] = [i for i in page["items"] if i["subject_id"] == old]
    assert row["stuck"]["reason"] == "worker_env"


def test_setting_moves_the_cutoff(client: TestClient, tmp_path: Path) -> None:
    task = blocked_for(client, tmp_path, 2)
    assert stuck_of(client, task) is None
    res = client.put(f"/api/settings/{KEY}", json={"value": 3600})
    assert res.status_code == 200, res.text
    assert stuck_of(client, task)["threshold_s"] == 3600
    res = client.put(f"/api/settings/{KEY}", json={"value": 10})
    assert res.status_code == 422  # below the 60 s floor
    client.delete(f"/api/settings/{KEY}")
    assert stuck_of(client, task) is None
