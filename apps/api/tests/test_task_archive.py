import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "x" * 32
TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("awaiting_approval", "pending", "ready", "claimed", "blocked", "in_review")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {res.json()['token']}"}


def make(client: TestClient, agent: dict[str, str], state: str, **body: object) -> dict:
    """A task in `state`, reached through the real API."""
    extra = {"requires_approval": True} if state == "awaiting_approval" else {}
    task = client.post(
        "/api/tasks",
        json={"project": "global", "title": state, "max_attempts": 1, **extra, **body},
    ).json()
    tid = task["id"]
    if state in ("awaiting_approval", "ready"):
        return task
    if state == "cancelled":
        return client.post(f"/api/tasks/{tid}/cancel").json()
    if state == "pending":
        dep = client.post("/api/tasks", json={"project": "global", "title": "d"})
        return client.put(
            f"/api/tasks/{tid}/dependencies",
            json={
                "depends_on": [dep.json()["id"]],
                "expected_version": task["version"]
                if "version" in task
                else task["updated_at"],
            },
        ).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    assert claimed["id"] == tid
    attempt = claimed["attempt"]
    if state == "claimed":
        return claimed
    if state == "blocked":
        path, payload = "block", {"attempt": attempt, "reason": "worker_env"}
    elif state == "failed":
        path, payload = (
            "fail",
            {"attempt": attempt, "error": "boom", "reason": "session_lost"},
        )
    else:
        path, payload = "complete", {"attempt": attempt, "result": {"summary": "ok"}}
        if state == "in_review":
            pytest.fail("use review=human")
    res = client.post(f"/api/tasks/{tid}/{path}", json=payload, headers=agent)
    return res.json()


def listed(client: TestClient, query: str = "") -> list[str]:
    return [t["id"] for t in client.get(f"/api/tasks?{query}").json()["items"]]


@pytest.mark.parametrize("state", TERMINAL)
def test_terminal_states_can_be_archived(
    client: TestClient, agent: dict[str, str], state: str
) -> None:
    task = make(client, agent, state)
    assert task["state"] == state
    res = client.post(f"/api/tasks/{task['id']}/archive")
    assert res.status_code == 200
    assert res.json()["archived_at"] is not None
    assert res.json()["state"] == state  # archiving is not a transition


@pytest.mark.parametrize(
    "state", ["awaiting_approval", "pending", "ready", "claimed", "blocked"]
)
def test_active_tasks_are_rejected_without_side_effects(
    client: TestClient, agent: dict[str, str], state: str
) -> None:
    task = make(client, agent, state)
    assert task["state"] == state
    before = client.get(f"/api/tasks/{task['id']}").json()
    events = client.get(f"/api/events?subject_id={task['id']}").json()
    res = client.post(f"/api/tasks/{task['id']}/archive")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "task_not_terminal"
    assert state in detail["message"]
    assert client.get(f"/api/tasks/{task['id']}").json() == before
    assert client.get(f"/api/events?subject_id={task['id']}").json() == events
    assert task["id"] in listed(client)


def test_in_review_is_rejected(client: TestClient, agent: dict[str, str]) -> None:
    task = client.post(
        "/api/tasks", json={"project": "global", "title": "r", "review": "human"}
    ).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    res = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "ok"}},
        headers=agent,
    )
    assert res.json()["state"] == "in_review"
    res = client.post(f"/api/tasks/{task['id']}/archive")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "task_not_terminal"


def test_unknown_task_is_404(client: TestClient) -> None:
    assert client.post("/api/tasks/nope/archive").status_code == 404


def test_archive_is_idempotent(client: TestClient, agent: dict[str, str]) -> None:
    task = make(client, agent, "failed")
    first = client.post(f"/api/tasks/{task['id']}/archive").json()
    events = client.get(f"/api/events?subject_id={task['id']}").json()
    second = client.post(f"/api/tasks/{task['id']}/archive")
    assert second.status_code == 200
    assert second.json() == first
    assert client.get(f"/api/events?subject_id={task['id']}").json() == events
    kinds = [e["kind"] for e in events]
    assert kinds.count("task.archived") == 1


def test_archived_tasks_leave_the_default_list_and_show_under_the_filter(
    client: TestClient, agent: dict[str, str]
) -> None:
    keep = make(client, agent, "ready")
    gone = make(client, agent, "cancelled")
    client.post(f"/api/tasks/{gone['id']}/archive")
    assert listed(client) == [keep["id"]]
    assert client.get("/api/tasks").json()["total"] == 1
    assert listed(client, "archived=true") == [gone["id"]]
    assert listed(client, "archived=true&state=failed") == []
    assert client.get(f"/api/tasks/{gone['id']}").json()["state"] == "cancelled"
    batch = client.get(f"/api/tasks?ids={gone['id']}").json()
    assert [t["id"] for t in batch["items"]] == [gone["id"]]
    assert batch["items"][0]["archived_at"] is not None


def test_unarchive_restores_and_is_idempotent(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = make(client, agent, "failed")
    client.post(f"/api/tasks/{task['id']}/archive")
    res = client.post(f"/api/tasks/{task['id']}/unarchive")
    assert res.status_code == 200
    assert res.json()["archived_at"] is None
    assert listed(client) == [task["id"]]
    assert listed(client, "archived=true") == []
    assert client.post(f"/api/tasks/{task['id']}/unarchive").status_code == 200
    kinds = [
        e["kind"] for e in client.get(f"/api/events?subject_id={task['id']}").json()
    ]
    assert kinds.count("task.unarchived") == 1


def test_unarchive_is_admin_only(client: TestClient, agent: dict[str, str]) -> None:
    task = make(client, agent, "failed")
    client.post(f"/api/tasks/{task['id']}/archive")
    res = client.post(f"/api/tasks/{task['id']}/unarchive", headers=agent)
    assert res.status_code == 403
    assert client.get(f"/api/tasks/{task['id']}").json()["archived_at"] is not None


def test_workers_cannot_archive(client: TestClient, agent: dict[str, str]) -> None:
    task = make(client, agent, "failed")
    assert (
        client.post(f"/api/tasks/{task['id']}/archive", headers=agent).status_code
        == 403
    )
    assert (
        client.post(
            "/api/tasks/archive", json={"states": ["failed"]}, headers=agent
        ).status_code
        == 403
    )


def test_retry_of_an_archived_failed_task_makes_it_active_again(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = make(client, agent, "failed")
    client.post(f"/api/tasks/{task['id']}/archive")
    res = client.post(f"/api/tasks/{task['id']}/retry")
    assert (res.json()["state"], res.json()["archived_at"]) == ("ready", None)
    assert listed(client) == [task["id"]]


def test_bulk_dry_run_counts_without_writing(
    client: TestClient, agent: dict[str, str]
) -> None:
    ids = [make(client, agent, "failed")["id"] for _ in range(2)]
    make(client, agent, "cancelled")
    make(client, agent, "ready")
    res = client.post(
        "/api/tasks/archive", json={"states": ["failed"], "dry_run": True}
    )
    assert res.json() == {"count": 2, "dry_run": True}
    assert len(listed(client)) == 4
    assert listed(client, "archived=true") == []

    res = client.post("/api/tasks/archive", json={"states": ["failed"]})
    assert res.json() == {"count": 2, "dry_run": False}
    assert sorted(listed(client, "archived=true")) == sorted(ids)
    # Repeating finds nothing left to archive.
    assert (
        client.post("/api/tasks/archive", json={"states": ["failed"]}).json()["count"]
        == 0
    )


def test_bulk_filters_by_project_and_age(
    client: TestClient, agent: dict[str, str], tmp_path: Path
) -> None:
    client.put("/api/projects/other", json={"name": "Other"})
    old = make(client, agent, "failed")
    recent = make(client, agent, "failed")
    elsewhere = make(client, agent, "failed", project="other")
    with sqlite3.connect(tmp_path / "argus.db") as conn:
        conn.execute(
            "UPDATE tasks SET updated_at = '2020-01-01T00:00:00.000000Z' WHERE id IN (?, ?)",
            (old["id"], elsewhere["id"]),
        )
    body = {"states": ["failed"], "project": "global", "older_than_days": 30}
    assert (
        client.post("/api/tasks/archive", json={**body, "dry_run": True}).json()[
            "count"
        ]
        == 1
    )
    assert client.post("/api/tasks/archive", json=body).json()["count"] == 1
    assert listed(client, "archived=true") == [old["id"]]
    assert set(listed(client)) == {recent["id"], elsewhere["id"]}


@pytest.mark.parametrize("state", ACTIVE)
def test_bulk_refuses_non_terminal_states_and_changes_nothing(
    client: TestClient, agent: dict[str, str], state: str
) -> None:
    failed = make(client, agent, "failed")
    res = client.post("/api/tasks/archive", json={"states": ["failed", state]})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "state_not_archivable"
    assert listed(client) == [failed["id"]]


def test_bulk_requires_a_state(client: TestClient) -> None:
    assert client.post("/api/tasks/archive", json={}).status_code == 422
    assert client.post("/api/tasks/archive", json={"states": []}).status_code == 422


def test_archived_tasks_leave_overview_and_ops_totals(
    client: TestClient, agent: dict[str, str]
) -> None:
    gone = [make(client, agent, "failed"), make(client, agent, "cancelled")]
    make(client, agent, "ready")
    assert client.get("/api/overview").json()["tasks"]["failed"] == 1
    for task in gone:
        client.post(f"/api/tasks/{task['id']}/archive")
    tasks = client.get("/api/overview").json()["tasks"]
    assert (tasks["failed"], tasks["cancelled"], tasks["ready"]) == (0, 0, 1)
    assert sum(tasks.values()) == 1
    metrics = client.get("/api/metrics").text
    assert 'argus_tasks{state="failed"} 0' in metrics
    assert 'argus_tasks{state="ready"} 1' in metrics
    client.post(f"/api/tasks/{gone[0]['id']}/unarchive")
    assert client.get("/api/overview").json()["tasks"]["failed"] == 1
