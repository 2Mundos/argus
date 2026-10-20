"""Page envelope, cursors, projections and the inbox (MCP operations 3.1, 3.2)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from argus_api.paging import TEXT_CAP
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "p" * 32


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
    return {"Authorization": f"Bearer {res.json()['token']}"}


def create(client: TestClient, project: str = "alpha", **extra) -> dict:
    res = client.post("/api/tasks", json={"title": "t", "project": project, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def ok(res) -> dict:
    assert res.status_code == 200, res.text
    return res.json()


def walk(client: TestClient, path: str, **params) -> tuple[list[dict], list[int]]:
    """Follow next_cursor to the end; return every item and each page's total."""
    items, totals, cursor = [], [], None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        page = ok(client.get(path, params={**params, **extra}))
        items += page["items"]
        totals.append(page["total"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            return items, totals
        cursor = page["next_cursor"]


def test_task_pages_cover_every_match_once(client: TestClient) -> None:
    ids = [create(client)["id"] for _ in range(7)]
    create(client, "beta")
    items, totals = walk(client, "/api/tasks", project="alpha", limit=3)
    assert [t["id"] for t in items] == ids[::-1]  # newest first
    assert totals == [7, 7, 7]  # a page is never mistaken for the total
    assert ok(client.get("/api/tasks", params={"limit": 1000}))["total"] == 8


def test_cursor_is_bound_to_its_filters(client: TestClient) -> None:
    for _ in range(3):
        create(client)
    page = ok(client.get("/api/tasks", params={"project": "alpha", "limit": 1}))
    res = client.get(
        "/api/tasks", params={"project": "beta", "cursor": page["next_cursor"]}
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "cursor_mismatch"
    res = client.get("/api/tasks", params={"cursor": "garbage!"})
    assert res.json()["detail"]["code"] == "invalid_cursor"


def test_summary_leaves_bodies_out_and_says_so(client: TestClient) -> None:
    dep = create(client)
    task = create(client, spec="x" * 100, depends_on=[dep["id"]])
    page = ok(client.get("/api/tasks", params={"state": "pending"}))
    [row] = page["items"]
    assert row["id"] == task["id"]
    assert row["depends_on"] == [dep["id"]]
    assert row["version"] == task["updated_at"]
    assert "spec" not in row and "result" not in row
    assert {"spec", "result", "input", "acceptance"} <= set(page["omitted"])


def test_detail_caps_long_text(client: TestClient) -> None:
    task = create(client, spec="é" * TEXT_CAP)  # 2 bytes each: twice the cap
    page = ok(client.get("/api/tasks", params={"view": "detail"}))
    [row] = page["items"]
    assert len(row["spec"].encode()) <= TEXT_CAP
    assert row["truncated"] == {"spec": 2 * TEXT_CAP}
    assert page["omitted"] == ["spec"]
    full = ok(client.get(f"/api/tasks/{task['id']}"))  # a single read is whole
    assert len(full["spec"]) == TEXT_CAP


def test_batch_read_keeps_order_and_reports_missing(client: TestClient) -> None:
    a, b = create(client)["id"], create(client)["id"]
    page = ok(client.get("/api/tasks", params={"ids": f"{b},nope,{a}"}))
    assert [t["id"] for t in page["items"]] == [b, a]
    assert page["missing"] == ["nope"]
    res = client.get("/api/tasks", params={"ids": a, "project": "alpha"})
    assert res.json()["detail"]["code"] == "ids_exclusive"
    res = client.get("/api/tasks", params={"ids": ",".join(f"t{n}" for n in range(51))})
    assert res.status_code == 422


def test_decisions_filter_by_project_and_kind(client: TestClient) -> None:
    create(client, requires_approval=True)
    create(client, "beta", requires_approval=True)
    page = ok(client.get("/api/decisions", params={"project": "alpha"}))
    assert page["total"] == 1
    [row] = page["items"]
    assert (row["kind"], row["project"]) == ("approval", "alpha")
    assert "question" not in row and "question" in page["omitted"]
    detail = ok(client.get("/api/decisions", params={"view": "detail"}))
    assert all(d["question"] for d in detail["items"])
    none = ok(client.get("/api/decisions", params={"kind": "review"}))
    assert none["total"] == 0


def ask(client: TestClient, worker: dict, question: str = "which?") -> dict:
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    res = client.post(
        f"/api/tasks/{claimed['id']}/ask",
        json={
            "attempt": claimed["attempt"],
            "question": question,
            "options": ["a", "b"],
        },
        headers=worker,
    )
    assert res.status_code == 201
    return res.json()


def test_inbox_joins_tasks_with_their_decisions(client: TestClient) -> None:
    worker = register(client)
    asked_task = create(client)
    question = ask(client, worker)
    review_task = create(client, review="orchestrator")
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    artifact = client.post(
        f"/api/tasks/{claimed['id']}/artifacts",
        params={"attempt": claimed["attempt"], "name": "report.md", "role": "report"},
        content=b"verdict: pass",
        headers={**worker, "Content-Type": "text/markdown"},
    ).json()
    client.post(
        f"/api/tasks/{claimed['id']}/complete",
        json={
            "attempt": claimed["attempt"],
            "result": {"summary": "s", "artifacts": [artifact["id"]]},
        },
        headers=worker,
    )
    create(client, "beta", requires_approval=True)
    create(client)  # ready: not waiting on anyone

    page = ok(client.get("/api/inbox"))
    assert page["total"] == 3
    assert page["totals_by_state"] == {
        "awaiting_approval": 1,
        "blocked": 1,
        "in_review": 1,
    }
    assert page["totals_by_decision_kind"] == {
        "approval": 1,
        "question": 1,
        "review": 1,
    }
    rows = {r["subject_id"]: r for r in page["items"]}
    assert rows[asked_task["id"]]["decision_id"] == question["id"]
    assert rows[asked_task["id"]]["audience"] == "any"
    assert rows[review_task["id"]]["decision_kind"] == "review"
    assert rows[review_task["id"]]["artifact_ids"] == [artifact["id"]]
    assert rows[review_task["id"]]["task"] is None  # summary
    assert {"task", "decision"} <= set(page["omitted"])

    alpha = ok(client.get("/api/inbox", params={"project": "alpha"}))
    assert alpha["total"] == 2
    reviews = ok(client.get("/api/inbox", params={"decision_kind": "review"}))
    assert [r["subject_id"] for r in reviews["items"]] == [review_task["id"]]

    detail = ok(client.get("/api/inbox", params={"state": "blocked", "view": "detail"}))
    [row] = detail["items"]
    assert row["decision"]["question"] == "which?"
    assert row["task"]["id"] == asked_task["id"]


def test_inbox_pages_and_non_task_decisions(client: TestClient) -> None:
    for _ in range(5):
        create(client, requires_approval=True)
    client.put(
        "/api/pools/web",
        json={"image": "example/w:1", "desired_replicas": 0, "max_replicas": 1},
    )
    orchestrator = register(client, "orchestrator")
    client.post(
        "/api/pools/web/scale", json={"desired_replicas": 3}, headers=orchestrator
    )
    items, totals = walk(client, "/api/inbox", limit=2)
    assert totals == [6, 6, 6]
    assert len({r["decision_id"] for r in items}) == 6
    pool = next(r for r in items if r["subject_type"] == "pool")
    assert (pool["subject_id"], pool["decision_kind"], pool["state"]) == (
        "web",
        "policy",
        None,
    )
    blocked_only = ok(client.get("/api/inbox", params={"state": "awaiting_approval"}))
    assert blocked_only["total"] == 5  # `state` keeps only task rows


def test_inbox_needs_a_manager(client: TestClient) -> None:
    worker = register(client)
    assert client.get("/api/inbox", headers=worker).status_code == 403
    assert client.get("/api/inbox", headers=register(client, "orchestrator")).is_success
