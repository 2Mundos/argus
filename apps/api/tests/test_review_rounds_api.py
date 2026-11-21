"""Review rounds on the parent task and the board filter (pre-merge independent review 3.7)."""

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "r" * 32
HEAD_1 = "a" * 40
HEAD_2 = "b" * 40


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, Path]]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        c.put("/api/projects/alpha", json={"name": "alpha"})
        yield c, tmp_path / "argus.db"


def make(client: TestClient, **extra) -> str:
    if extra.get("kind") == "code_review":
        extra["input"] = {"task_id": extra.get("parent_id", "x"), "head_sha": HEAD_1}
    res = client.post("/api/tasks", json={"title": "t", "project": "alpha", **extra})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def finish(db: Path, child: str, data: dict | None, model: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET state = 'succeeded', result = ? WHERE id = ?",
            (json.dumps({"summary": "r", "data": data}), child),
        )
        conn.execute(
            "INSERT INTO model_executions (id, task_id, key, attempt, layer, state,"
            " provider, configured_model, recorded_at, updated_at)"
            " VALUES (?, ?, '0', 1, 'harness', 'succeeded', 'openai', ?, ?, ?)",
            (
                f"{child}-x",
                child,
                model,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )


@pytest.fixture
def reviewed(env: tuple[TestClient, Path]) -> tuple[TestClient, str, list[str]]:
    client, db = env
    parent = make(client)
    first = make(client, kind="code_review", parent_id=parent)
    second = make(client, kind="code_review", parent_id=parent)
    finish(
        db,
        first,
        {
            "verdict": "request_changes",
            "head_sha": HEAD_1,
            "findings": [
                "P2: boundary check",
                {"priority": "P1", "file": "a.py", "line": 7, "description": "leak"},
                {"file": "b.py", "message": "nit"},
            ],
        },
        "gpt-x",
    )
    finish(
        db, second, {"verdict": "approve", "head_sha": HEAD_2, "findings": []}, "gpt-y"
    )
    return client, parent, [first, second]


def test_reviews_list_rounds_oldest_first(
    reviewed: tuple[TestClient, str, list[str]],
) -> None:
    client, parent, (first, second) = reviewed
    res = client.get(f"/api/tasks/{parent}/reviews")
    assert res.status_code == 200, res.text
    one, two = res.json()["rounds"]
    assert (one["child_id"], one["round"], one["verdict"]) == (
        first,
        1,
        "request_changes",
    )
    assert one["head_sha"] == HEAD_1
    assert one["findings_by_priority"] == {"P2": 1, "P1": 1, "unranked": 1}
    assert one["findings"][1] == {
        "priority": "P1",
        "file": "a.py",
        "lines": "7",
        "description": "leak",
    }
    assert one["reviewer_model"] == "gpt-x"
    assert one["finished_at"] is not None
    assert (two["child_id"], two["round"], two["verdict"]) == (second, 2, "approve")
    assert two["head_sha"] == HEAD_2
    assert two["reviewer_model"] == "gpt-y"
    assert client.get(f"/api/tasks/{first}/reviews").json()["rounds"] == []
    assert client.get("/api/tasks/nope/reviews").status_code == 404


def test_board_filter_is_opt_in_and_pages_correctly(
    reviewed: tuple[TestClient, str, list[str]],
) -> None:
    client, parent, children = reviewed
    orphan = make(client, kind="code_review")
    other = make(client)

    def ids(**params) -> tuple[list[str], int]:
        out, cursor = [], None
        while True:
            page = client.get(
                "/api/tasks",
                params={**params, "limit": 1, **({"cursor": cursor} if cursor else {})},
            ).json()
            out += [i["id"] for i in page["items"]]
            if not page["has_more"]:
                return out, page["total"]
            cursor = page["next_cursor"]

    every, total = ids()
    assert set(every) == {parent, *children, orphan, other} and total == 5
    shown, total = ids(hide_review_children=True)
    assert set(shown) == {parent, orphan, other} and total == 3
    page = client.get("/api/tasks", params={"hide_review_children": True}).json()
    badges = {i["id"]: i.get("review_badge") for i in page["items"]}
    assert badges[parent] == {"round": 2, "verdict": "approve", "state": "succeeded"}
    assert badges[other] is None
    default = client.get("/api/tasks").json()
    assert all("review_badge" not in i for i in default["items"])


def test_voided_round_is_void_in_rounds_and_board_badge(
    env: tuple[TestClient, Path],
) -> None:
    client, db = env
    parent = make(client)
    child = make(client, kind="code_review", parent_id=parent)
    finish(db, child, {"verdict": "approve", "head_sha": HEAD_1, "findings": []}, "m")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
            " VALUES ('2026-01-01T00:00:00Z', 'system', 'task', ?,"
            " 'review.independent_void', ?)",
            (parent, json.dumps({"child_id": child, "reason": "same_model"})),
        )
    rounds = client.get(f"/api/tasks/{parent}/reviews").json()["rounds"]
    assert rounds[0]["verdict"] == "void"
    page = client.get("/api/tasks", params={"hide_review_children": True}).json()
    badge = {i["id"]: i.get("review_badge") for i in page["items"]}[parent]
    assert badge == {"round": 1, "verdict": "void", "state": "succeeded"}


def test_head_moved_void_without_child_id_voids_the_reviewed_round(
    env: tuple[TestClient, Path],
) -> None:
    client, db = env
    parent = make(client)
    old = make(client, kind="code_review", parent_id=parent)
    new = make(client, kind="code_review", parent_id=parent)
    finish(db, old, {"verdict": "approve", "head_sha": HEAD_1, "findings": []}, "m")
    finish(db, new, {"verdict": "approve", "head_sha": HEAD_2, "findings": []}, "m")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
            " VALUES ('2999-01-01T00:00:00Z', 'system', 'task', ?,"
            " 'review.independent_void', ?)",
            (parent, json.dumps({"reason": "head_moved", "reviewed": HEAD_1})),
        )
    rounds = client.get(f"/api/tasks/{parent}/reviews").json()["rounds"]
    assert [r["verdict"] for r in rounds] == ["void", "approve"]
    page = client.get("/api/tasks", params={"hide_review_children": True}).json()
    assert {i["id"]: i.get("review_badge") for i in page["items"]}[parent][
        "verdict"
    ] == "approve"


def test_ids_batch_keeps_review_badge(
    reviewed: tuple[TestClient, str, list[str]],
) -> None:
    client, parent, _ = reviewed
    page = client.get("/api/tasks", params={"ids": parent}).json()
    assert page["items"][0]["review_badge"]["round"] == 2
