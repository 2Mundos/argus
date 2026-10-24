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


def test_assignee_lists_what_an_agent_holds_now(client: TestClient) -> None:
    held = {}
    for _ in range(2):
        create(client)
        boot = client.post("/api/agents/bootstrap-tokens", json={}).json()
        reg = ok_created(
            client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
        )
        auth = {"Authorization": f"Bearer {reg['token']}"}
        task = ok(client.post("/api/tasks/claim", json={"wait_s": 0}, headers=auth))
        held[reg["agent"]["id"]] = task["id"]
    for agent_id, task_id in held.items():
        page = ok(client.get("/api/tasks", params={"assignee": agent_id}))
        assert [t["id"] for t in page["items"]] == [task_id]
    res = client.get("/api/tasks", params={"ids": task_id, "assignee": agent_id})
    assert res.json()["detail"]["code"] == "ids_exclusive"


def ok_created(res) -> dict:
    assert res.status_code == 201, res.text
    return res.json()


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


# -- groups, impact, blocking_only and order (MCP operations 3.2) ---------------------


def seed_impact(client: TestClient) -> dict:
    """alpha: `big` is blocked by a question and holds two pending dependents; `small`
    is awaiting approval with one dependent. beta: `solo` awaits approval, no dependents.
    A pool decision rounds it out."""
    worker = register(client)
    big = create(client, title="big")
    question = ask(client, worker, "which?")
    create(client, depends_on=[big["id"]], title="dep-1")
    create(client, depends_on=[big["id"]], title="dep-2")
    small = create(client, title="small", requires_approval=True)
    create(client, depends_on=[small["id"]], title="dep-3")
    solo = create(client, "beta", title="solo", requires_approval=True)
    client.put(
        "/api/pools/web",
        json={"image": "example/w:1", "desired_replicas": 0, "max_replicas": 1},
    )
    client.post(
        "/api/pools/web/scale",
        json={"desired_replicas": 3},
        headers=register(client, "orchestrator"),
    )
    return {"big": big, "small": small, "solo": solo, "question": question}


def test_inbox_groups_by_project_and_task_with_age_and_impact(
    client: TestClient,
) -> None:
    ids = seed_impact(client)
    page = ok(client.get("/api/inbox"))
    groups = {g["key"]: g for g in page["groups"]}
    big, small, solo = (f"task:{ids[k]['id']}" for k in ("big", "small", "solo"))
    assert set(groups) == {big, small, solo, "pool"}
    assert (groups[big]["project"], groups[big]["title"]) == ("alpha", "big")
    # the blocked task itself plus its two non-terminal dependents
    assert (groups[big]["open_decisions"], groups[big]["stalled_tasks"]) == (1, 3)
    assert (groups[small]["open_decisions"], groups[small]["stalled_tasks"]) == (1, 1)
    assert (groups[solo]["project"], groups[solo]["stalled_tasks"]) == ("beta", 0)
    assert (groups["pool"]["project"], groups["pool"]["subject_id"]) == (None, None)
    assert all(g["oldest_age_s"] >= 0 for g in groups.values())

    rows = {r["subject_id"]: r for r in page["items"]}
    row = rows[ids["big"]["id"]]
    assert row["group"] == big and row["decision_kind"] == "question"
    assert row["impact"] == 3
    assert [b["title"] for b in row["blocks"]] == ["big", "dep-1", "dep-2"]
    assert rows[ids["solo"]["id"]]["blocks"] == []
    assert rows["web"]["blocks"] == [] and rows["web"]["impact"] == 0


def test_inbox_group_age_is_the_oldest_row(client: TestClient) -> None:
    ids = seed_impact(client)
    # Two rows in one task group cannot exist (one open decision per task), so the
    # group age equals its row age; check it against the row.
    page = ok(client.get("/api/inbox"))
    row = next(r for r in page["items"] if r["subject_id"] == ids["big"]["id"])
    group = next(g for g in page["groups"] if g["key"] == row["group"])
    assert group["oldest_age_s"] >= row["age_s"] >= 0


def test_blocking_only_keeps_rows_that_block_a_task(client: TestClient) -> None:
    ids = seed_impact(client)
    page = ok(client.get("/api/inbox", params={"blocking_only": True}))
    assert {r["subject_id"] for r in page["items"]} == {
        ids["big"]["id"],
        ids["small"]["id"],
    }
    assert page["total"] == 2
    assert {g["key"] for g in page["groups"]} == {
        f"task:{ids['big']['id']}",
        f"task:{ids['small']['id']}",
    }
    assert sum(page["totals_by_state"].values()) == 2


def test_inbox_sorts_by_impact_or_age(client: TestClient) -> None:
    ids = seed_impact(client)
    by_age = ok(client.get("/api/inbox"))
    assert [r["subject_id"] for r in by_age["items"]][:2] == [
        ids["big"]["id"],
        ids["small"]["id"],
    ]  # oldest first
    by_impact = ok(client.get("/api/inbox", params={"order": "impact"}))
    assert [r["impact"] for r in by_impact["items"]] == [3, 1, 0, 0]
    assert by_impact["items"][0]["subject_id"] == ids["big"]["id"]
    # impact ties fall back to age: solo (older) before the pool decision
    assert [r["subject_id"] for r in by_impact["items"]][2:] == [
        ids["solo"]["id"],
        "web",
    ]
    assert [g["stalled_tasks"] for g in by_impact["groups"]] == [3, 1, 0, 0]
    # paging in impact order visits the same rows
    items, totals = walk(client, "/api/inbox", order="impact", limit=1)
    assert [r["subject_id"] for r in items] == [
        r["subject_id"] for r in by_impact["items"]
    ]
    assert totals == [4, 4, 4, 4]
    bad = client.get("/api/inbox", params={"order": "nope"})
    assert bad.status_code == 422


def test_inbox_cursor_is_bound_to_order_and_blocking_filter(client: TestClient) -> None:
    seed_impact(client)
    page = ok(client.get("/api/inbox", params={"limit": 1, "order": "impact"}))
    res = client.get("/api/inbox", params={"cursor": page["next_cursor"]})
    assert res.status_code == 400, res.text
    assert "cursor_mismatch" in res.text


def test_inbox_dependents_that_finished_do_not_count(client: TestClient) -> None:
    worker = register(client)
    big = create(client, title="big")
    ask(client, worker, "which?")
    done = create(client, depends_on=[big["id"]], title="gone")
    client.post(f"/api/tasks/{done['id']}/cancel").raise_for_status()
    row = next(r for r in ok(client.get("/api/inbox"))["items"])
    assert [b["title"] for b in row["blocks"]] == ["big"]
