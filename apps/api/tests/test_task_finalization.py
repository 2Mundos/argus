"""Execution versus follow-up of an attempt (task-finalization.spec.md)."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks.finalization import CHECKPOINT_STALE_S, derive
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "x" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


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


def claim(client: TestClient, worker: dict[str, str]) -> dict:
    client.post("/api/tasks", json={"project": "global", "title": "t"})
    res = client.post("/api/tasks/claim", json={}, headers=worker)
    assert res.status_code == 200, res.text
    return res.json()


def report(client, worker, task, attempt, state, detail=None):
    return client.post(
        f"/api/tasks/{task['id']}/attempts/{attempt}/checkpoint",
        json={"state": state, "detail": detail},
        headers=worker,
    )


def fin(client: TestClient, task: dict) -> dict | None:
    return client.get(f"/api/tasks/{task['id']}").json()["finalization"]


def test_execution_then_failed_follow_up_is_not_success(client: TestClient) -> None:
    worker = register(client)
    task = claim(client, worker)
    assert fin(client, task)["step"] == "executing"
    # The claim is still open: a follow-up cannot have started.
    assert report(client, worker, task, 1, "running").status_code == 409

    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": 1, "result": {"summary": "ok"}},
        headers=worker,
    )
    assert done.status_code == 200, done.text
    f = fin(client, task)
    assert (f["step"], f["claim_end_action"], f["checkpoint"]) == (
        "finished",
        "complete",
        None,
    )
    assert f["execution_s"] is not None

    assert report(client, worker, task, 1, "saved").status_code == 409  # not started
    assert report(client, worker, task, 1, "running").status_code == 204
    assert report(client, worker, task, 1, "running").status_code == 204  # repeat
    f = fin(client, task)
    assert (f["step"], f["checkpoint"]["state"]) == ("finalizing_checkpoint", "running")
    assert f["checkpoint"]["duration_s"] is None

    assert (
        report(client, worker, task, 1, "failed", "env_checkpoint_limit").status_code
        == 204
    )
    f = fin(client, task)
    assert (f["step"], f["checkpoint"]["state"]) == ("finished", "failed")
    assert f["checkpoint"]["detail"] == "env_checkpoint_limit"
    assert f["checkpoint"]["duration_s"] is not None
    # Terminal and fixed: the same report repeats, a different one is refused.
    assert (
        report(client, worker, task, 1, "failed", "env_checkpoint_limit").status_code
        == 204
    )
    assert report(client, worker, task, 1, "saved").status_code == 409
    # The outcome of the attempt is untouched by the follow-up.
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "succeeded"


def test_report_is_fenced_to_the_attempt_and_its_holder(client: TestClient) -> None:
    first = register(client)
    other = register(client)
    task = claim(client, first)
    client.post(f"/api/tasks/{task['id']}/release", json={"reason": "x"})
    assert (
        report(client, other, task, 1, "running").status_code == 409
    )  # not the holder
    assert (
        report(client, first, task, 2, "running").status_code == 404
    )  # no such attempt

    # A newer attempt (another worker) is claimed; the stale report only touches attempt 1.
    second = client.post("/api/tasks/claim", json={}, headers=other)
    assert second.status_code == 200, second.text
    assert second.json()["attempt"] == 2
    assert report(client, first, task, 1, "running").status_code == 204
    assert fin(client, task)["step"] == "executing"  # attempt 2 unaffected
    assert fin(client, task)["checkpoint"] is None


def test_expiry_and_release_give_no_execution_duration(client: TestClient) -> None:
    worker = register(client)
    task = claim(client, worker)
    client.post(f"/api/tasks/{task['id']}/release", json={"reason": "x"})
    f = fin(client, task)
    assert (f["step"], f["claim_end_action"], f["execution_s"]) == (
        "finished",
        "release",
        None,
    )


def test_silent_running_follow_up_is_unknown_not_a_result() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    started = now - timedelta(seconds=CHECKPOINT_STALE_S)
    row = {
        "attempt": 1,
        "claimed_at": (started - timedelta(hours=1)).isoformat(),
        "claim_ended_at": started.isoformat(),
        "claim_end_action": "fail",
        "checkpoint_state": "running",
        "checkpoint_started_at": started.isoformat(),
        "checkpoint_ended_at": None,
        "checkpoint_detail": None,
    }
    assert derive(row, "ready", now).step == "finalizing_checkpoint"  # type: ignore[arg-type,union-attr]
    late = derive(row, "ready", now + timedelta(seconds=1))  # type: ignore[arg-type]
    assert (late.step, late.checkpoint.state, late.checkpoint.duration_s) == (  # type: ignore[union-attr]
        "unknown",
        "unknown",
        None,
    )
    # An attempt from before the migration has nothing recorded.
    legacy = {**row, "claim_ended_at": None, "checkpoint_state": None}
    assert derive(legacy, "succeeded", now) is None  # type: ignore[arg-type]
