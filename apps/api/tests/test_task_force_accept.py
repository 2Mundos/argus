"""Owner "Accept and close" (docs/specs/task-deliverable.spec.md 3.10.1)."""

import json
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argus_api import mcp_server
from argus_api.main import app
from argus_api.tasks import delivery
from argus_core.delivery import Delivery, FactOrigin, PrState, ShaFact
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, tool
from test_mcp import register as register_mcp
from test_task_archive import make

NOW = datetime(2026, 11, 27, tzinfo=UTC)
PR = "https://github.com/2Mundos/argus/pull/513"
HEAD = "a" * 40
OPEN_STATES = ["awaiting_approval", "pending", "ready", "claimed", "blocked"]
TERMINAL = ["succeeded", "failed", "cancelled"]


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


def force(client: TestClient, task_id: str, **body: object):
    return client.post(
        f"/api/tasks/{task_id}/force-accept", json={"reason": "owner merged it", **body}
    )


def review_task(client: TestClient, agent: dict[str, str], **body: object) -> dict:
    task = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "r", "review": "human", **body},
    ).json()
    claimed = client.post("/api/tasks/claim", json={}, headers=agent).json()
    assert claimed["id"] == task["id"]
    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "ok"}},
        headers=agent,
    ).json()
    assert done["state"] == "in_review"
    return done


def rows(tmp_path: Path, sql: str, *args: object) -> list[tuple]:
    with sqlite3.connect(tmp_path / "argus.db") as conn:
        return conn.execute(sql, args).fetchall()


@pytest.mark.parametrize("state", OPEN_STATES)
def test_every_open_state_succeeds_as_force_accepted(
    client: TestClient, agent: dict[str, str], state: str
) -> None:
    task = make(client, agent, state)
    assert task["state"] == state
    res = force(client, task["id"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "succeeded"
    disposition = body["review_disposition"]
    assert disposition["outcome"] == "force_accepted"
    assert disposition["reason"] == "owner merged it"
    assert disposition["decided_by"].startswith("admin")


def test_in_review_succeeds_as_force_accepted(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = review_task(client, agent)
    res = force(client, task["id"])
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "succeeded"
    assert res.json()["review_disposition"]["outcome"] == "force_accepted"


@pytest.mark.parametrize("state", TERMINAL)
def test_a_finished_task_answers_task_not_open_and_is_unchanged(
    client: TestClient, agent: dict[str, str], state: str
) -> None:
    task = make(client, agent, state)
    before = client.get(f"/api/tasks/{task['id']}").json()
    res = force(client, task["id"])
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "task_not_open"
    assert client.get(f"/api/tasks/{task['id']}").json() == before


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_a_blank_or_missing_reason_is_422(
    client: TestClient, agent: dict[str, str], reason: str | None
) -> None:
    task = make(client, agent, "ready")
    body = {} if reason is None else {"reason": reason}
    res = client.post(f"/api/tasks/{task['id']}/force-accept", json=body)
    assert res.status_code == 422
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "ready"


def test_a_stale_version_is_refused(client: TestClient, agent: dict[str, str]) -> None:
    task = make(client, agent, "ready")
    res = force(client, task["id"], expected_version="2020-01-01T00:00:00.000000Z")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "stale_version"
    current = client.get(f"/api/tasks/{task['id']}").json()
    assert (
        force(client, task["id"], expected_version=current["version"]).status_code
        == 200
    )


def test_a_worker_may_not_force_accept(
    client: TestClient, agent: dict[str, str]
) -> None:
    task = make(client, agent, "ready")
    res = client.post(
        f"/api/tasks/{task['id']}/force-accept",
        json={"reason": "x"},
        headers=agent,
    )
    assert res.status_code in (401, 403)


def test_the_claim_is_released_without_a_failure(
    client: TestClient, agent: dict[str, str], tmp_path: Path
) -> None:
    task = make(client, agent, "claimed")
    body = force(client, task["id"]).json()
    assert body["state"] == "succeeded"
    assert body["failures"] == 0
    assert body["assignee_agent_id"] is None
    assert body["lease_expires_at"] is None
    assert rows(
        tmp_path,
        "SELECT outcome FROM task_attempts WHERE task_id = ?",
        task["id"],
    ) == [("released",)]
    # the holder's next write is refused
    res = client.post(
        f"/api/tasks/{task['id']}/progress",
        json={"attempt": task["attempt"], "message": "still here"},
        headers=agent,
    )
    assert res.status_code == 409


def test_open_decisions_are_closed_not_approved(
    client: TestClient, agent: dict[str, str], tmp_path: Path
) -> None:
    task = review_task(client, agent)
    before = rows(
        tmp_path,
        "SELECT state FROM decisions WHERE subject_id = ?",
        task["id"],
    )
    assert before == [("open",)]
    assert force(client, task["id"]).status_code == 200
    assert rows(
        tmp_path,
        "SELECT state FROM decisions WHERE subject_id = ?",
        task["id"],
    ) == [("cancelled",)]


def test_review_children_are_cancelled_and_no_reviewer_is_spawned(
    client: TestClient, agent: dict[str, str], tmp_path: Path
) -> None:
    parent = review_task(client, agent)
    child = client.post(
        "/api/tasks",
        json={
            "project": "demo",
            "title": "review",
            "kind": "code_review",
            "parent_id": parent["id"],
            "input": {"task_id": parent["id"], "head_sha": HEAD},
        },
    )
    assert child.status_code in (200, 201), child.text
    assert force(client, parent["id"]).status_code == 200
    assert rows(
        tmp_path,
        "SELECT state FROM tasks WHERE parent_id = ? ORDER BY rowid",
        parent["id"],
    ) == [("cancelled",)]


def test_unmet_items_stay_verification_missing(
    client: TestClient, agent: dict[str, str], tmp_path: Path
) -> None:
    task = review_task(client, agent, acceptance=["it works"])
    body = force(client, task["id"]).json()
    assert body["state"] == "succeeded"
    assert body["review_disposition"]["unmet"]
    states = rows(
        tmp_path,
        "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.force_accepted'",
        task["id"],
    )
    assert json.loads(states[0][0])["unmet"] == body["review_disposition"]["unmet"]
    checklist = client.get(f"/api/tasks/{task['id']}/continuation").json()["checklist"]
    assert [i["status"] for i in checklist] == ["verification_missing"]


def test_dependents_become_ready(client: TestClient, agent: dict[str, str]) -> None:
    task = review_task(client, agent)
    dependent = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "after", "depends_on": [task["id"]]},
    ).json()
    assert dependent["state"] == "pending"
    assert force(client, task["id"]).status_code == 200
    assert client.get(f"/api/tasks/{dependent['id']}").json()["state"] == "ready"


def test_the_event_records_actor_reason_prior_state_and_pr(
    client: TestClient,
    agent: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = review_task(client, agent)

    async def seen(conn, checker, t) -> Delivery:
        return Delivery(
            pr=PR,
            pr_state=PrState.MERGED,
            merged_at=NOW,
            checked_at=NOW,
            flags=[],
            accept_decision_id=None,
            accept_blocked=None,
            head=ShaFact(sha=HEAD, source=FactOrigin.FORGE, checked_at=NOW),
        )

    monkeypatch.setattr(delivery, "delivery_of", seen)
    assert force(client, task["id"]).status_code == 200
    (row,) = rows(
        tmp_path,
        "SELECT actor, data FROM events WHERE subject_id = ?"
        " AND kind = 'task.force_accepted'",
        task["id"],
    )
    data = json.loads(row[1])
    assert row[0].startswith("admin")
    assert data["reason"] == "owner merged it"
    assert data["prior_state"] == "in_review"
    assert data["from"] == "in_review" and data["to"] == "succeeded"
    assert data["pr"] == {"url": PR, "state": "merged", "head_sha": HEAD}


def test_the_forge_is_only_read_never_written(
    client: TestClient, agent: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class ReadOnlyChecker:
        def __getattr__(self, name: str):
            raise AssertionError(f"forge call {name}")

    task = review_task(client, agent)
    monkeypatch.setattr(app.state, "pr_checker", ReadOnlyChecker())
    assert force(client, task["id"]).status_code == 200


def test_no_setting_or_other_gate_changes(
    client: TestClient, agent: dict[str, str], tmp_path: Path
) -> None:
    other = review_task(client, agent)
    forced = review_task(client, agent)
    settings = rows(tmp_path, "SELECT * FROM settings")
    assert force(client, forced["id"]).status_code == 200
    assert rows(tmp_path, "SELECT * FROM settings") == settings
    assert client.get(f"/api/tasks/{other['id']}").json()["state"] == "in_review"
    assert rows(
        tmp_path, "SELECT state FROM decisions WHERE subject_id = ?", other["id"]
    ) == [("open",)]


def test_the_mcp_tool_calls_the_route(
    client: TestClient, agent: dict[str, str]
) -> None:
    assert "force_accept_task" in mcp_server.audiences
    task = make(client, agent, "ready")
    orchestrator = register_mcp(client, "orchestrator")
    assert "reason" in tool(
        client, orchestrator, "force_accept_task", task_id=task["id"], reason=""
    ).get("error", "reason")
    done = tool(
        client,
        orchestrator,
        "force_accept_task",
        task_id=task["id"],
        reason="owner verified",
    )
    assert done["state"] == "succeeded"
    assert done["review_disposition"]["outcome"] == "force_accepted"
    worker = register_mcp(client)
    assert (
        "requires scope"
        in tool(client, worker, "force_accept_task", task_id="t", reason="x")["error"]
    )
