"""Bounded task search via GET /api/tasks?q= (docs/specs/task-discovery.spec.md)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN as MCP_ADMIN
from test_mcp import tool

ADMIN_TOKEN = MCP_ADMIN


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        c.put("/api/projects/alpha", json={"name": "alpha"})
        c.put("/api/projects/beta", json={"name": "beta"})
        yield c


def create(
    client: TestClient, title: str = "t", project: str = "alpha", **extra
) -> dict:
    res = client.post("/api/tasks", json={"title": title, "project": project, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def ok(res) -> dict:
    assert res.status_code == 200, res.text
    return res.json()


def walk(client: TestClient, **params) -> tuple[list[dict], list[int]]:
    items, totals, cursor = [], [], None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        page = ok(client.get("/api/tasks", params={**params, **extra}))
        items += page["items"]
        totals.append(page["total"])
        if not page["has_more"]:
            return items, totals
        cursor = page["next_cursor"]


def register_worker(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    token = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_search_pages_with_stable_cursor_and_total(client: TestClient) -> None:
    ids = [create(client, title=f"discovery-{n} unique-marker")["id"] for n in range(7)]
    create(client, title="unrelated")
    items, totals = walk(client, q="unique-marker", limit=2)
    assert len(items) == 7
    assert totals == [7, 7, 7, 7]
    assert [t["id"] for t in items] == ids[::-1]
    assert all("matches" in t for t in items)
    assert all(any(m["field"] == "title" for m in t["matches"]) for t in items)


def test_terminal_and_archived_compose_with_q(client: TestClient) -> None:
    worker = register_worker(client)

    def finish(title: str, path: str, payload: dict) -> dict:
        task = create(client, title=title, max_attempts=1)
        claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
        assert claimed["id"] == task["id"]
        res = client.post(
            f"/api/tasks/{claimed['id']}/{path}",
            json={"attempt": claimed["attempt"], **payload},
            headers=worker,
        )
        assert res.status_code == 200, res.text
        return res.json()

    succeeded = finish(
        "search-term done-one", "complete", {"result": {"summary": "ok"}}
    )
    failed = finish(
        "search-term fail-one",
        "fail",
        {"error": "boom", "reason": "session_lost"},
    )
    cancelled = create(client, title="search-term cancel-one")
    client.post(f"/api/tasks/{cancelled['id']}/cancel")

    archived = finish(
        "search-term archive-one", "complete", {"result": {"summary": "ok"}}
    )
    client.post(f"/api/tasks/{archived['id']}/archive")

    page = ok(
        client.get(
            "/api/tasks",
            params=[
                ("q", "search-term"),
                ("state", "succeeded"),
                ("state", "failed"),
                ("state", "cancelled"),
            ],
        )
    )
    by_id = {t["id"]: t["state"] for t in page["items"]}
    assert by_id[succeeded["id"]] == "succeeded"
    assert by_id[failed["id"]] == "failed"
    assert by_id[cancelled["id"]] == "cancelled"
    assert archived["id"] not in by_id

    archived_page = ok(
        client.get(
            "/api/tasks",
            params={"q": "search-term", "archived": True},
        )
    )
    assert [t["id"] for t in archived_page["items"]] == [archived["id"]]


def test_cursor_mismatch_when_q_or_state_changes(client: TestClient) -> None:
    for n in range(3):
        create(client, title=f"cursor-mark-{n}")
    page = ok(client.get("/api/tasks", params={"q": "cursor-mark", "limit": 1}))
    assert page["next_cursor"]
    res = client.get(
        "/api/tasks",
        params={"q": "other-mark", "cursor": page["next_cursor"]},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "cursor_mismatch"
    res = client.get(
        "/api/tasks",
        params={"q": "cursor-mark", "state": "failed", "cursor": page["next_cursor"]},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "cursor_mismatch"


def test_worker_permission_boundary_on_items_and_total(client: TestClient) -> None:
    worker = register_worker(client)
    leased = create(client, title="scope-visible leased-task")
    other = create(client, title="scope-visible other-task")
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == leased["id"]
    page = ok(
        client.get(
            "/api/tasks",
            params={"q": "scope-visible"},
            headers=worker,
        )
    )
    assert page["total"] == 1
    assert [t["id"] for t in page["items"]] == [leased["id"]]
    assert other["id"] not in {t["id"] for t in page["items"]}


def test_false_positives_and_substring_behavior(client: TestClient) -> None:
    hit_title = create(client, title="alpha-widget", spec="unrelated body")
    hit_spec = create(client, title="other", spec="contains alpha-widget here")
    create(
        client,
        title="noise",
        spec="clean",
        input={"note": "alpha-widget only in input"},
    )
    page = ok(client.get("/api/tasks", params={"q": "alpha-widget"}))
    ids = {t["id"] for t in page["items"]}
    assert ids == {hit_title["id"], hit_spec["id"]}
    by_id = {t["id"]: t for t in page["items"]}
    assert {m["field"] for m in by_id[hit_title["id"]]["matches"]} == {"title"}
    assert {m["field"] for m in by_id[hit_spec["id"]]["matches"]} == {"spec"}
    # every term must match: second term only in the title hit
    page = ok(client.get("/api/tasks", params={"q": "alpha-widget body"}))
    assert [t["id"] for t in page["items"]] == [hit_title["id"]]

    # LIKE wildcards are literals: % and _ must not match arbitrarily
    pct = create(client, title="pct-hit", spec="50% paid")
    create(client, title="pct-miss", spec="50x paid")
    page = ok(client.get("/api/tasks", params={"q": "50%"}))
    assert [t["id"] for t in page["items"]] == [pct["id"]]
    # `_` in q is literal; unescaped LIKE `_` would match the X in snakeXcase_name
    create(client, title="underscore-lit", spec="snakeXcase_name")
    page = ok(client.get("/api/tasks", params={"q": "snake_case_name"}))
    assert page["items"] == [] and page["total"] == 0


def test_no_matches_vs_snippet_truncation(client: TestClient) -> None:
    empty = ok(client.get("/api/tasks", params={"q": "zzz-no-such-term"}))
    assert empty["total"] == 0 and empty["items"] == []

    long = "prefix-" + ("needle" + "x" * 400) + "-suffix"
    create(client, title="short", spec=long)
    page = ok(client.get("/api/tasks", params={"q": "needle"}))
    [row] = page["items"]
    [m] = [x for x in row["matches"] if x["field"] == "spec"]
    assert m["truncated"] is True
    assert len(m["snippet"]) <= 200
    assert "needle" in m["snippet"]
    assert m["snippet"].startswith("...") or m["snippet"].endswith("...")


def test_invalid_q_bounds(client: TestClient) -> None:
    res = client.get("/api/tasks", params={"q": "   "})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "query_empty"
    res = client.get(
        "/api/tasks",
        params={"q": " ".join(f"t{n}" for n in range(9))},
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "query_too_broad"
    res = client.get("/api/tasks", params={"q": "x" * 65})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "query_too_broad"


def test_matches_absent_when_q_absent(client: TestClient) -> None:
    create(client, title="plain")
    page = ok(client.get("/api/tasks"))
    assert page["total"] >= 1
    assert all("matches" not in t for t in page["items"])


def test_mcp_list_tasks_forwards_q(client: TestClient) -> None:
    create(client, title="mcp-search-hit")
    page = tool(client, ADMIN_TOKEN, "list_tasks", q="mcp-search-hit")
    assert page["total"] == 1
    assert page["items"][0]["matches"]
    assert "matches" in page["items"][0]
