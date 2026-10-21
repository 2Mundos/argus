"""Workers may only read leased work (worker-read-scope spec)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "r" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        for slug in ("alpha", "beta"):
            c.put(f"/api/projects/{slug}", json={"name": slug})
        yield c


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201
    return {"Authorization": f"Bearer {res.json()['token']}"}


def create_task(client: TestClient, project: str = "alpha", **extra) -> dict:
    res = client.post(
        "/api/tasks", json={"title": "t", "project": project, "spec": "secret", **extra}
    )
    assert res.status_code == 201, res.text
    return res.json()


def claim_complete(
    client: TestClient, worker: dict[str, str], *, with_artifact: bool = False
) -> tuple[dict, dict | None]:
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    artifact = None
    if with_artifact:
        artifact = client.post(
            f"/api/tasks/{claimed['id']}/artifacts",
            params={"attempt": claimed["attempt"], "name": "out.txt"},
            content=b"private-bytes",
            headers={**worker, "Content-Type": "text/plain"},
        ).json()
        result = {"summary": "done", "artifacts": [artifact["id"]]}
    else:
        result = {"summary": "done"}
    done = client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={"attempt": claimed["attempt"], "result": result},
        headers=worker,
    )
    assert done.status_code == 200, done.text
    return claimed, artifact


def test_worker_cannot_read_another_workers_task_spec(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    create_task(client, "alpha")
    claimed, _ = claim_complete(client, alice)

    ok = client.get(f"/api/tasks/{claimed['id']}", headers=alice)
    assert ok.status_code == 200
    assert ok.json()["spec"] == "secret"

    denied = client.get(f"/api/tasks/{claimed['id']}", headers=bob)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "not_leased"

    listed = client.get("/api/tasks", headers=bob).json()
    assert listed["total"] == 0
    assert listed["items"] == []

    batch = client.get(f"/api/tasks?ids={claimed['id']}", headers=bob).json()
    assert batch["items"] == []
    assert claimed["id"] in batch["missing"]


def test_worker_cannot_read_another_workers_artifact(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    create_task(client, "alpha")
    claimed, artifact = claim_complete(client, alice, with_artifact=True)
    assert artifact is not None

    assert (
        client.get(f"/api/artifacts/{artifact['id']}", headers=alice).status_code == 200
    )
    denied = client.get(f"/api/artifacts/{artifact['id']}", headers=bob)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "not_leased"

    content = client.get(f"/api/artifacts/{artifact['id']}/content", headers=bob)
    assert content.status_code == 403

    listed = client.get(f"/api/tasks/{claimed['id']}/artifacts", headers=bob)
    assert listed.status_code == 403


def test_worker_event_list_is_project_scoped(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    create_task(client, "alpha")
    claimed, _ = claim_complete(client, alice)

    alice_events = client.get(
        "/api/events", params={"subject_id": claimed["id"]}, headers=alice
    ).json()
    assert any(e["kind"] == "task.claim" for e in alice_events)

    bob_events = client.get(
        "/api/events", params={"subject_id": claimed["id"]}, headers=bob
    ).json()
    assert bob_events == []

    # Fleet-wide subjects stay hidden from workers with no leased projects.
    pools = client.get("/api/events", params={"subject_type": "pool"}, headers=bob)
    assert pools.json() == []


def test_worker_knowledge_is_project_scoped(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    page = client.post(
        "/api/knowledge/pages",
        json={
            "project": "alpha",
            "slug": "runbook",
            "title": "Runbook",
            "summary": "how",
            "body": "classified",
            "tags": [],
        },
    ).json()["page"]

    create_task(client, "alpha")
    claim_complete(client, alice)

    assert client.get(f"/api/knowledge/pages/{page['id']}", headers=alice).is_success
    denied = client.get(f"/api/knowledge/pages/{page['id']}", headers=bob)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "project_scope"

    assert client.get("/api/knowledge/pages", headers=bob).json() == []
    assert (
        client.get(
            "/api/knowledge/search", params={"q": "classified"}, headers=bob
        ).json()
        == []
    )
    assert [p["slug"] for p in client.get("/api/projects", headers=bob).json()] == []
    assert "alpha" in [
        p["slug"] for p in client.get("/api/projects", headers=alice).json()
    ]


def test_worker_cannot_read_foreign_decision(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    task = create_task(client, "alpha")
    claimed = client.post("/api/tasks/claim", json={}, headers=alice).json()
    decision = client.post(
        f"/api/tasks/{claimed['id']}/ask",
        json={
            "attempt": claimed["attempt"],
            "question": "which?",
            "options": ["a", "b"],
        },
        headers=alice,
    ).json()

    assert (
        client.get(f"/api/decisions/{decision['id']}", headers=alice).status_code == 200
    )
    denied = client.get(f"/api/decisions/{decision['id']}", headers=bob)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "not_leased"

    bob_list = client.get("/api/decisions", headers=bob).json()
    assert bob_list["total"] == 0
    assert all(d["id"] != decision["id"] for d in bob_list["items"])
    # alice still sees her task's decision while it is open
    alice_list = client.get(
        "/api/decisions", params={"subject_id": task["id"]}, headers=alice
    ).json()
    assert alice_list["total"] == 1


def test_orchestrator_and_admin_keep_global_reads(client: TestClient) -> None:
    worker, orch = register(client), register(client, "orchestrator")
    create_task(client, "beta")
    claimed, artifact = claim_complete(client, worker, with_artifact=True)
    page = client.post(
        "/api/knowledge/pages",
        json={
            "project": "beta",
            "slug": "notes",
            "title": "Notes",
            "summary": "s",
            "body": "body",
            "tags": [],
        },
    ).json()["page"]

    for headers in (None, orch):  # None → fixture default admin
        kw = {} if headers is None else {"headers": headers}
        assert client.get(f"/api/tasks/{claimed['id']}", **kw).status_code == 200
        assert client.get(f"/api/artifacts/{artifact['id']}", **kw).status_code == 200
        assert client.get(f"/api/knowledge/pages/{page['id']}", **kw).status_code == 200
        events = client.get(
            "/api/events", params={"subject_id": claimed["id"]}, **kw
        ).json()
        assert any(e["kind"] == "task.claim" for e in events)


def test_previous_lease_still_allows_read_after_complete(client: TestClient) -> None:
    worker = register(client)
    create_task(client, "alpha")
    claimed, _ = claim_complete(client, worker)
    # After complete the assignee is cleared, but the grant remains.
    res = client.get(f"/api/tasks/{claimed['id']}", headers=worker)
    assert res.status_code == 200
    assert res.json()["state"] == "succeeded"
    assert res.json()["assignee_agent_id"] is None


def _event_kinds(events: list[dict]) -> set[tuple[str, str]]:
    return {(e["subject_type"], e["subject_id"]) for e in events}


def test_worker_events_limited_to_leased_tasks_in_project(client: TestClient) -> None:
    alice = register(client)
    task_a = create_task(client, "alpha", title="a")
    claimed = client.post("/api/tasks/claim", json={}, headers=alice).json()
    # A second, unleased task in the same project, plus a decision on it.
    task_b = create_task(client, "alpha", title="b")
    leased_id = claimed["id"]
    other_id = task_b["id"] if leased_id == task_a["id"] else task_a["id"]
    assert leased_id != other_id

    listed = client.get("/api/events", params={"limit": 500}, headers=alice).json()
    subjects = _event_kinds(listed)
    assert ("task", leased_id) in subjects
    assert ("task", other_id) not in subjects
    assert (
        client.get("/api/events", params={"subject_id": other_id}, headers=alice).json()
        == []
    )

    waited = client.get("/api/events/wait", params={"after": 0}, headers=alice).json()
    assert ("task", leased_id) in _event_kinds(waited["events"])
    assert ("task", other_id) not in _event_kinds(waited["events"])
    assert (
        client.get(
            "/api/events/wait",
            params={"after": 0, "subject_id": other_id},
            headers=alice,
        ).json()["events"]
        == []
    )

    # Admin still sees the unleased task's events.
    admin = client.get("/api/events", params={"subject_id": other_id}).json()
    assert admin


def test_worker_decision_events_limited_to_leased_tasks(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    create_task(client, "alpha")
    claimed = client.post("/api/tasks/claim", json={}, headers=alice).json()
    decision = client.post(
        f"/api/tasks/{claimed['id']}/ask",
        json={"attempt": claimed["attempt"], "question": "q?", "options": ["a", "b"]},
        headers=alice,
    ).json()
    # Bob leases another task in the same project.
    create_task(client, "alpha")
    assert client.post("/api/tasks/claim", json={}, headers=bob).is_success

    def decision_ids(headers: dict[str, str]) -> set[str]:
        events = client.get(
            "/api/events", params={"subject_type": "decision"}, headers=headers
        ).json()
        return {e["subject_id"] for e in events}

    assert decision["id"] in decision_ids(alice)
    assert decision["id"] not in decision_ids(bob)


def test_live_stream_filter_matches_replay_rule(client: TestClient) -> None:
    from datetime import UTC, datetime

    from argus_api.eventlog.routes import _live_allowed
    from argus_api.events import Event

    alice = register(client)
    create_task(client, "alpha", title="a")
    claimed = client.post("/api/tasks/claim", json={}, headers=alice).json()
    other = create_task(client, "alpha", title="b")
    agent_id = claimed["assignee_agent_id"]
    db = client.app.state.db  # type: ignore[attr-defined]
    assert client.portal is not None

    def allowed(task_id: str) -> bool:
        event = Event(1, datetime.now(UTC), "x", "task", task_id, "task.updated", {})
        return client.portal.call(_live_allowed, db, event, {"alpha"}, agent_id)  # type: ignore[union-attr]

    assert allowed(claimed["id"])
    assert not allowed(other["id"])


def test_worker_can_read_page_pinned_on_leased_task(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    page = client.post(
        "/api/knowledge/pages",
        json={
            "project": "beta",
            "slug": "pinned",
            "title": "Pinned",
            "summary": "s",
            "body": "pinned-body",
            "tags": [],
        },
    ).json()["page"]
    create_task(client, "alpha", knowledge=[page["id"]])
    claimed = client.post("/api/tasks/claim", json={}, headers=alice).json()
    assert [r["page_id"] for r in claimed["knowledge_refs"]] == [page["id"]]

    res = client.get(f"/api/knowledge/pages/{page['id']}", headers=alice)
    assert res.status_code == 200, res.text
    assert client.get(
        f"/api/knowledge/pages/{page['id']}/revisions", headers=alice
    ).is_success
    denied = client.get(f"/api/knowledge/pages/{page['id']}", headers=bob)
    assert denied.status_code == 403
