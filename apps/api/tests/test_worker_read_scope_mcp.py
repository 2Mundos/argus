"""Worker read scope through REST edit/attempt routes and the real MCP tools."""

import pytest
import test_mcp
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, register, tool
from test_task_edit_api import bearer, make, mcp_edit, rest_edit

client = test_mcp.client  # the same app fixture as the MCP tests


def lease_and_finish(client: TestClient, token: str) -> tuple[dict, dict]:
    claimed = client.post("/api/tasks/claim", json={}, headers=bearer(token)).json()
    artifact = client.post(
        f"/api/tasks/{claimed['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "out.txt"},
        content=b"private-bytes",
        headers={**bearer(token), "Content-Type": "text/plain"},
    ).json()
    done = client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "ok"}},
        headers=bearer(token),
    )
    assert done.status_code == 200, done.text
    return claimed, artifact


@pytest.fixture
def leased(client: TestClient) -> tuple[str, str, dict, dict]:
    alice, bob = register(client), register(client)
    make(client, spec="secret")
    task, artifact = lease_and_finish(client, alice)
    return alice, bob, task, artifact


def test_attempts_route_is_scoped_to_leased_work(
    client: TestClient, leased: tuple[str, str, dict, dict]
) -> None:
    alice, bob, task, _ = leased
    path = f"/api/tasks/{task['id']}/attempts"
    ok = client.get(path, headers=bearer(alice))
    assert ok.status_code == 200
    assert ok.json()
    denied = client.get(path, headers=bearer(bob))
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "not_leased"
    assert client.get(path, headers=bearer(ADMIN_TOKEN)).status_code == 200


def test_rest_edit_is_refused_for_every_worker(
    client: TestClient, leased: tuple[str, str, dict, dict]
) -> None:
    alice, bob, task, _ = leased
    current = client.get(f"/api/tasks/{task['id']}", headers=bearer(alice)).json()
    for token in (alice, bob):  # leased or not: edit is never widened to workers
        assert rest_edit(client, token, current, title="x").status_code == 403
    after = client.get(f"/api/tasks/{task['id']}", headers=bearer(ADMIN_TOKEN)).json()
    assert after["title"] == current["title"]
    assert after["version"] == current["version"]


def test_mcp_edit_task_is_refused_for_every_worker(
    client: TestClient, leased: tuple[str, str, dict, dict]
) -> None:
    alice, bob, task, _ = leased
    current = client.get(f"/api/tasks/{task['id']}", headers=bearer(alice)).json()
    for token in (alice, bob):
        assert "error" in mcp_edit(client, token, current, title="x")
    after = client.get(f"/api/tasks/{task['id']}", headers=bearer(ADMIN_TOKEN)).json()
    assert after["title"] == current["title"]


def test_mcp_reads_are_scoped_to_leased_work(
    client: TestClient, leased: tuple[str, str, dict, dict]
) -> None:
    alice, bob, task, artifact = leased

    assert tool(client, alice, "get_task", task_id=task["id"])["spec"] == "secret"
    assert "error" in tool(client, bob, "get_task", task_id=task["id"])

    mine = tool(client, alice, "get_tasks", ids=[task["id"]], view="detail")
    assert [t["id"] for t in mine["items"]] == [task["id"]]
    theirs = tool(client, bob, "get_tasks", ids=[task["id"]], view="detail")
    assert theirs["items"] == []
    assert task["id"] in theirs["missing"]

    listing = tool(client, alice, "list_artifacts", task_id=task["id"])
    assert [a["id"] for a in listing] == [artifact["id"]]
    assert "error" in tool(client, bob, "list_artifacts", task_id=task["id"])

    text = tool(client, alice, "read_artifact", artifact_id=artifact["id"])
    assert "private-bytes" in str(text)
    assert "error" in tool(client, bob, "read_artifact", artifact_id=artifact["id"])
    assert "error" in tool(client, bob, "verify_artifact", artifact_id=artifact["id"])


def test_event_stream_hides_leased_task_events_from_other_workers(
    client: TestClient, leased: tuple[str, str, dict, dict]
) -> None:
    alice, bob, task, _ = leased
    params = {"subject_id": task["id"]}
    mine = client.get("/api/events", params=params, headers=bearer(alice)).json()
    assert any(e["kind"] == "task.claim" for e in mine)
    assert client.get("/api/events", params=params, headers=bearer(bob)).json() == []
