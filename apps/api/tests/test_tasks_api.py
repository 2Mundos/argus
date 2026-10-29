import sqlite3
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from argus_api.db import Database
from argus_api.main import app
from argus_api.tasks.views import LIST_COLUMNS, Refs, TaskSummary
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "x" * 32
NO_AUTH = {"Authorization": ""}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    """A client authenticated as admin by default; pass headers to act as others."""
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()[
        "token"
    ]
    res = client.post("/api/agents/register", json={"bootstrap_token": boot})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return register(client)


def test_create_get_list(client: TestClient) -> None:
    res = client.post(
        "/api/tasks", json={"project": "global", "title": "write docs", "priority": 2}
    )
    assert res.status_code == 201
    task = res.json()
    assert task["state"] == "ready"
    assert task["created_by"] == "admin:preset"
    assert client.get(f"/api/tasks/{task['id']}").json()["title"] == "write docs"
    assert [t["id"] for t in client.get("/api/tasks?state=ready").json()["items"]] == [
        task["id"]
    ]
    assert client.get("/api/tasks?state=failed").json()["total"] == 0
    summary = client.get("/api/tasks").json()["items"][0]
    assert summary["title"] == "write docs"
    assert "spec" not in summary
    assert (
        "spec" in client.get("/api/tasks", params={"view": "detail"}).json()["items"][0]
    )


def test_claim_and_complete(client: TestClient, agent: dict[str, str]) -> None:
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    assert claimed["id"] == task["id"]
    res = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "done"}},
        headers=agent,
    )
    assert res.status_code == 200
    assert res.json()["state"] == "succeeded"


def test_claim_with_nothing_ready_returns_204(
    client: TestClient, agent: dict[str, str]
) -> None:
    res = client.post("/api/tasks/claim", json={"wait_s": 0}, headers=agent)
    assert res.status_code == 204


def test_claim_long_poll_wakes_when_a_task_becomes_ready(
    client: TestClient, agent: dict[str, str]
) -> None:
    with ThreadPoolExecutor(1) as pool:
        started = time.monotonic()
        waiting = pool.submit(
            client.post, "/api/tasks/claim", json={"wait_s": 10}, headers=agent
        )
        time.sleep(0.3)
        task = client.post(
            "/api/tasks", json={"project": "global", "title": "late"}
        ).json()
        res = waiting.result(timeout=5)
    assert res.status_code == 200
    assert res.json()["id"] == task["id"]
    assert time.monotonic() - started < 5


def test_worker_operations_require_an_agent(client: TestClient) -> None:
    res = client.post("/api/tasks/claim", json={})  # admin token, but not an agent
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "forbidden"


def test_invalid_token_is_401_not_anonymous(client: TestClient) -> None:
    res = client.post(
        "/api/tasks",
        json={"project": "global", "title": "x"},
        headers={"Authorization": "Bearer nope"},
    )
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "invalid_token"


def test_agent_lifecycle_over_http(client: TestClient, agent: dict[str, str]) -> None:
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    hb = client.post(
        "/api/agents/me/heartbeat",
        json={"leases": [{"task_id": task["id"], "attempt": claimed["attempt"]}]},
        headers=agent,
    ).json()
    assert [r["task_id"] for r in hb["renewed"]] == [task["id"]]
    assert hb["heartbeat_interval_s"] == 30
    [listed] = client.get("/api/agents").json()
    assert listed["claimed_tasks"] == 1

    left = client.post("/api/agents/me/leave", headers=agent).json()
    assert left["state"] == "gone"
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "ready"
    assert (
        client.post("/api/agents/me/heartbeat", json={}, headers=agent).status_code
        == 401
    )


def test_errors_have_actionable_codes(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    client.post("/api/tasks/claim", json={}, headers=agent)
    res = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": 7, "result": {"summary": "x"}},
        headers=agent,
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "stale_attempt"

    res = client.post(f"/api/tasks/{task['id']}/unblock")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "invalid_transition"

    res = client.get("/api/tasks/missing")
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "not_found"


def test_a_worker_blocks_only_for_the_environment(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    url = f"/api/tasks/{task['id']}/block"
    attempt = claimed["attempt"]
    res = client.post(url, json={"attempt": attempt, "reason": "bored"}, headers=agent)
    assert res.status_code == 422
    res = client.post(
        url,
        json={"attempt": attempt, "reason": "env_repository_unreachable", "error": "x"},
        headers=agent,
    )
    assert (res.json()["state"], res.json()["error"]) == ("blocked", "x")


WORKSPACE_REASONS = [
    "env_workspace_unconfigured",
    "env_workspace_permission",
    "env_workspace_invalid",
    "env_workspace_remote_mismatch",
    "env_checkpoint_unavailable",
    "env_checkpoint_invalid",
    "env_checkpoint_conflict",
    "env_checkpoint_busy",
    "env_checkpoint_limit",
    "env_workspace_branch_mismatch",
    "env_workspace_limit",
]


@pytest.mark.parametrize("reason", WORKSPACE_REASONS)
def test_a_worker_blocks_for_a_broken_task_workspace(
    client: TestClient, agent: dict[str, str], reason: str
) -> None:
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    res = client.post(
        f"/api/tasks/{task['id']}/block",
        json={
            "attempt": claimed["attempt"],
            "reason": reason,
            "error": "origin is not set",
        },
        headers=agent,
    )
    body = res.json()
    assert (body["state"], body["block_reason"], body["failures"]) == (
        "blocked",
        reason,
        0,
    )


def _fail_once(client: TestClient, agent: dict[str, str], **body: object) -> dict:
    """Create a task, claim it, and fail it; with max_attempts=1 it ends `failed`."""
    task = client.post(
        "/api/tasks",
        json={"project": "global", "title": "x", "max_attempts": 1, **body},
    ).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    res = client.post(
        f"/api/tasks/{task['id']}/fail",
        json={"attempt": claimed["attempt"], "error": "quota exhausted"},
        headers=agent,
    )
    assert res.json()["state"] == "failed"
    return res.json()


def test_retry_returns_a_failed_task_to_ready(
    client: TestClient, agent: dict[str, str]
) -> None:
    failed = _fail_once(client, agent)
    res = client.post(f"/api/tasks/{failed['id']}/retry")
    assert res.status_code == 200
    task = res.json()
    assert task["state"] == "ready"
    assert (task["attempt"], task["failures"], task["max_attempts"]) == (0, 0, 1)
    assert task["error"] is None
    assert task["assignee_agent_id"] is None
    assert task["lease_expires_at"] is None
    assert task["progress_message"] is None
    assert task["progress_percent"] is None

    # Claimable through the normal flow, with a fresh attempt and failure budget.
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    assert (claimed["id"], claimed["attempt"]) == (failed["id"], 1)


def test_retry_refuses_any_state_but_failed(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    res = client.post(f"/api/tasks/{task['id']}/retry")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "task_not_failed"
    assert res.json()["detail"]["message"] == "task is ready, not failed"

    client.post("/api/tasks/claim", json={}, headers=agent)
    res = client.post(f"/api/tasks/{task['id']}/retry")
    assert res.json()["detail"]["message"] == "task is claimed, not failed"
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "claimed"

    assert client.post("/api/tasks/missing/retry").status_code == 404


def test_a_second_retry_in_a_row_is_refused(
    client: TestClient, agent: dict[str, str]
) -> None:
    failed = _fail_once(client, agent)
    assert client.post(f"/api/tasks/{failed['id']}/retry").status_code == 200
    res = client.post(f"/api/tasks/{failed['id']}/retry")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "task_not_failed"
    events = client.get(f"/api/events?subject_id={failed['id']}").json()
    assert [e["kind"] for e in events].count("task.retried") == 1


def test_retry_records_the_attempts_it_discarded(
    client: TestClient, agent: dict[str, str], tmp_path: Path
) -> None:
    failed = _fail_once(client, agent)
    # Review rejections raise `attempt` without counting as failures, so an
    # attempt count above max_attempts is reachable; force that shape here.
    with sqlite3.connect(tmp_path / "argus.db") as conn:
        conn.execute(
            "UPDATE tasks SET attempt = 4, max_attempts = 3, error = ? WHERE id = ?",
            ("e" * 2000, failed["id"]),
        )
    res = client.post(f"/api/tasks/{failed['id']}/retry")
    assert res.status_code == 200
    assert res.json()["attempt"] == 0
    events = client.get(f"/api/events?subject_id={failed['id']}").json()
    (event,) = [e for e in events if e["kind"] == "task.retried"]
    assert event["actor"] == "admin:preset"
    assert event["data"]["task_id"] == failed["id"]
    assert event["data"]["previous_attempts"] == 4
    assert event["data"]["max_attempts"] == 3
    assert event["data"]["previous_error"] == "e" * 500 + "…"


def test_retry_is_a_manager_operation(
    client: TestClient, agent: dict[str, str]
) -> None:
    failed = _fail_once(client, agent)
    res = client.post(f"/api/tasks/{failed['id']}/retry", headers=agent)
    assert res.status_code == 403
    assert client.get(f"/api/tasks/{failed['id']}").json()["state"] == "failed"


def test_progress_milestone_over_rest(
    client: TestClient, agent: dict[str, str]
) -> None:
    created = client.post("/api/tasks", json={"project": "global", "title": "t"}).json()
    legacy = client.get(f"/api/tasks/{created['id']}").json()
    assert (legacy["progress_kind"], legacy["progress_data"]) == (None, None)
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    url = f"/api/tasks/{claimed['id']}/progress"
    bad = client.post(
        url,
        json={"attempt": 1, "message": "m", "kind": "bogus", "data": {}},
        headers=agent,
    )
    assert bad.status_code == 422
    res = client.post(
        url,
        json={
            "attempt": 1,
            "message": "PR #3",
            "kind": "pr_opened",
            "data": {"number": 3, "url": "https://git.example.com/o/r/pulls/3"},
        },
        headers=agent,
    )
    assert res.status_code == 200
    assert res.json()["progress_kind"] == "pr_opened"
    summary = client.get("/api/tasks", params={"view": "summary"}).json()["items"][0]
    assert summary["progress_kind"] == "pr_opened"
    assert summary["progress_data"]["number"] == 3
    assert summary["progress_at"] is not None


def test_list_columns_skip_task_bodies() -> None:
    bodies = {"spec", "acceptance", "input", "required_capabilities", "result", "error"}
    assert bodies.isdisjoint(LIST_COLUMNS)


def test_list_columns_cover_summary_fields() -> None:
    computed = {"version", "models", "age_s", "truncated"} | set(Refs.model_fields)
    assert set(TaskSummary.model_fields) - computed <= set(LIST_COLUMNS)


async def test_newest_list_uses_partial_index(db: Database) -> None:
    # Archived COUNT(*) may use the older tasks_archived index; the ordered
    # lists and the active count need the newest-first indexes.
    queries = {
        "active": (
            "SELECT id FROM tasks WHERE archived_at IS NULL"
            " ORDER BY created_at DESC, id DESC LIMIT 101",
            "tasks_active_newest",
        ),
        "archived": (
            "SELECT id FROM tasks WHERE archived_at IS NOT NULL"
            " ORDER BY created_at DESC, id DESC LIMIT 101",
            "tasks_archived_newest",
        ),
        "active_count": (
            "SELECT count(*) FROM tasks WHERE archived_at IS NULL",
            "tasks_active_newest",
        ),
        "archived_count": (
            "SELECT count(*) FROM tasks WHERE archived_at IS NOT NULL",
            "tasks_archived",
        ),
    }
    async with db.read() as conn:
        for name, (sql, index) in queries.items():
            async with conn.execute("EXPLAIN QUERY PLAN " + sql) as cur:
                plan = " | ".join(row["detail"] for row in await cur.fetchall())
            assert index in plan, (name, plan)
