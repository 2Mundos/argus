"""The inbox's actionable projection (MCP operations 3.2, "Actionable projection")."""

import asyncio
import importlib
import json
from collections.abc import Generator
from pathlib import Path

import httpx2 as httpx
import pytest
from argus_api.db import ts
from argus_api.main import app
from fastapi.testclient import TestClient
from test_list_reads import ask, create, ok, register, walk
from test_mcp import tool
from test_task_delivery import URL, forge, review_task, setup_repository

# After argus_api.main: imported first, the module would hit a known import cycle.
inbox_actions = importlib.import_module("argus_api.inbox_actions")
ADMIN_TOKEN = "test-admin-token-" + "m" * 32  # test_mcp's, so `tool` works too
GH_TOKEN = "ghp_" + "a" * 36
SECRET = f"use {GH_TOKEN} with Authorization: Bearer abcdefghijklmnop"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        for slug in ("alpha", "beta"):
            c.put(f"/api/projects/{slug}", json={"name": slug})
        yield c


def actionable(client: TestClient, **params) -> dict:
    return ok(client.get("/api/inbox", params={"view": "actionable", **params}))


def row(page: dict, subject_id: str) -> dict:
    return next(r for r in page["items"] if r["subject_id"] == subject_id)


def pr_state(state: str, merged: bool = False):
    body = {"state": state, "merged": merged, "merged_at": None}
    if merged:
        body["merged_at"] = "2026-10-30T10:00:00+00:00"
    return lambda _: httpx.Response(200, json=body)


def store_pr(client: TestClient, tid: str, state: str) -> None:
    base = f"/api/tasks/{tid}/continuation"
    revision = ok(client.get(base))["revision"]
    patch = {
        "expected_revision": revision,
        "reason": "known PR",
        "artifacts": [{"kind": "pr", "ref": "12", "url": URL, "state": state}],
    }
    ok(client.patch(base, json=patch))


def test_default_shape_is_unchanged(client: TestClient) -> None:
    worker = register(client)
    create(client)
    ask(client, worker)
    page = ok(client.get("/api/inbox"))
    assert set(page) == {
        "items",
        "total",
        "has_more",
        "next_cursor",
        "omitted",
        "groups",
        "totals_by_state",
        "totals_by_decision_kind",
    }
    assert "actionable" not in page["items"][0]
    # A default cursor is not one of the projection's, and vice versa.
    create(client)
    ask(client, worker)
    first = ok(client.get("/api/inbox", params={"limit": 1}))
    res = client.get(
        "/api/inbox", params={"view": "actionable", "cursor": first["next_cursor"]}
    )
    assert res.json()["detail"]["code"] == "cursor_mismatch"


def test_open_question_says_who_answers_and_what_each_option_does(
    client: TestClient,
) -> None:
    worker = register(client)
    task = create(client, spec="Build the collector.\n\nMore detail.")
    question = ask(client, worker, "Which store?")
    page = actionable(client)
    item = row(page, task["id"])
    a = item["actionable"]
    assert item["decision_id"] == question["id"]
    assert item["task"] is None and item["decision"] is None
    assert a["question"] == {
        "text": "Which store?",
        "bytes": 12,
        "truncated": False,
        "ref": None,
    }
    assert a["spec_excerpt"]["text"].startswith("Build the collector.")
    assert a["options"] == ["a", "b"]
    assert [e["choice"] for e in a["effects"]] == ["a", "b"]
    assert "returns to ready" in a["effects"][0]["effect"]
    assert a["authority"] == {
        "audience": "any",
        "answerable_by": ["orchestrator", "admin"],
        "caller_role": "admin",
        "caller_may_attempt": True,
        "reason": "decision audience is any",
    }
    assert a["next"]["action"] == "answer_decision"
    assert a["next"]["args"] == {"decision_id": question["id"]}
    assert "re-check" in page["authority_note"]
    # Unknown is said, not left out.
    assert a["ci"]["status"] == "unknown" and a["ci"]["value"] is None
    assert a["transfer"]["status"] == "unknown"
    assert a["wip"]["status"] == "unknown"
    assert a["delivery"]["status"] == "unknown"
    assert a["delivery"]["live"]["error"] == "not_requested"
    assert page["external"]["requested"] is False
    assert page["external"]["in_snapshot"] is False


def test_blocked_task_without_decision_suggests_unblock(client: TestClient) -> None:
    worker = register(client)
    task = create(client)
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    res = client.post(
        f"/api/tasks/{task['id']}/block",
        json={"attempt": claimed["attempt"], "reason": "worker_env"},
        headers=worker,
    )
    assert res.status_code == 200, res.text
    a = row(actionable(client), task["id"])["actionable"]
    assert a["question"] is None and a["effects"] == []
    assert a["authority"]["audience"] is None
    assert a["authority"]["caller_may_attempt"] is True
    assert a["next"]["action"] == "unblock_task"
    assert a["next"]["args"] == {"task_id": task["id"]}
    assert "worker_env" in a["next"]["reason"]
    assert a["checklist"]["state"] in ("structured", "unstructured")


def test_human_decisions_are_escalated_for_an_orchestrator(client: TestClient) -> None:
    approval = create(client, requires_approval=True)
    review = create(client, review="human")
    worker = register(client)
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == review["id"]
    client.post(
        f"/api/tasks/{review['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "done"}},
        headers=worker,
    )
    orchestrator = register(client, "orchestrator")
    page = ok(
        client.get("/api/inbox", params={"view": "actionable"}, headers=orchestrator)
    )
    for tid in (approval["id"], review["id"]):
        a = row(page, tid)["actionable"]
        assert a["authority"]["answerable_by"] == ["admin"]
        assert a["authority"]["caller_role"] == "orchestrator"
        assert a["authority"]["caller_may_attempt"] is False
        assert a["next"]["action"] == "escalate_to_human"
    approve = row(page, approval["id"])["actionable"]["effects"]
    assert approve == [
        {"choice": "approve", "effect": "approves the task: it can be claimed"},
        {"choice": "reject", "effect": "cancels the task"},
    ]
    # The same rows, for an admin: may attempt, still only a hint.
    admin = row(actionable(client), approval["id"])["actionable"]
    assert admin["authority"]["caller_may_attempt"] is True
    assert admin["next"]["action"] == "answer_decision"
    assert ok(client.get(f"/api/tasks/{approval['id']}"))["state"] == (
        "awaiting_approval"  # nothing was approved by reading
    )


def test_orchestrator_review_is_answerable_by_an_orchestrator(
    client: TestClient,
) -> None:
    setup_repository(client)
    tid = review_task(client, "done")
    page = ok(
        client.get(
            "/api/inbox",
            params={"view": "actionable"},
            headers=register(client, "orchestrator"),
        )
    )
    a = row(page, tid)["actionable"]
    assert a["authority"]["audience"] == "orchestrator"
    assert a["authority"]["caller_may_attempt"] is True
    assert [e["choice"] for e in a["effects"]] == ["accept", "reject"]
    assert a["effects"][0]["effect"] == "the task succeeds"


def test_impact_counts_a_diamond_once(client: TestClient) -> None:
    # root <- b, c <- d (d depends on both b and c) <- e
    worker = register(client)
    root = create(client, title="root")
    ask(client, worker)
    b = create(client, title="b", depends_on=[root["id"]])
    c = create(client, title="c", depends_on=[root["id"]])
    d = create(client, title="d", depends_on=[b["id"], c["id"]])
    e = create(client, title="e", depends_on=[d["id"]])
    impact = row(actionable(client), root["id"])["actionable"]["impact"]
    assert impact["direct"]["count"] == 2
    assert {t["id"] for t in impact["direct"]["items"]} == {b["id"], c["id"]}
    assert impact["transitive"]["count"] == 2
    assert [t["id"] for t in impact["transitive"]["items"]] == [d["id"], e["id"]]
    assert impact["direct"]["omitted"] == impact["transitive"]["omitted"] == 0


def test_long_text_is_cut_with_a_whole_item_reference(client: TestClient) -> None:
    worker = register(client)
    task = create(client, spec="é" * 2000)
    question = ask(client, worker, "q" * 5000)
    a = row(actionable(client), task["id"])["actionable"]
    assert a["question"]["truncated"] is True
    assert a["question"]["bytes"] == 5000
    assert len(a["question"]["text"].encode()) <= inbox_actions.QUESTION_CAP
    assert a["question"]["ref"] == f"GET /api/decisions/{question['id']}"
    assert a["spec_excerpt"]["truncated"] is True
    assert len(a["spec_excerpt"]["text"].encode()) <= inbox_actions.EXCERPT_CAP
    assert a["spec_excerpt"]["ref"] == f"GET /api/tasks/{task['id']}"


def test_page_byte_budget_cuts_rows_and_resumes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = register(client)
    for _ in range(4):
        create(client)
        ask(client, worker, "x" * 1500)
    monkeypatch.setattr(inbox_actions, "PAGE_BYTES", 9000)
    page = actionable(client)
    assert 1 <= len(page["items"]) < 4
    assert page["has_more"] is True and "budget_cut" in page["omitted"]
    assert page["total"] == 4
    items, _ = walk(client, "/api/inbox", view="actionable")
    assert len({r["decision_id"] for r in items}) == 4  # every row once
    monkeypatch.setattr(inbox_actions, "PAGE_BYTES", 1)
    assert len(actionable(client)["items"]) == 1  # never an empty page


def test_large_set_pages_with_its_projection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(60):
        create(client, requires_approval=True)
    monkeypatch.setattr(inbox_actions, "PAGE_BYTES", 10**7)  # the row cap alone
    page = actionable(client, limit=100)
    assert len(page["items"]) == inbox_actions.MAX_LIMIT  # capped in this view
    items, totals = walk(client, "/api/inbox", view="actionable", limit=25)
    assert totals == [60, 60, 60]
    assert len({r["subject_id"] for r in items}) == 60
    # The cursor carries the projection options.
    res = client.get(
        "/api/inbox",
        params={"view": "actionable", "forge": "true", "cursor": page["next_cursor"]},
    )
    assert res.json()["detail"]["code"] == "cursor_mismatch"


def test_authorization_and_redaction(client: TestClient) -> None:
    worker = register(client)
    assert (
        client.get("/api/inbox", params={"view": "actionable"}, headers=worker)
    ).status_code == 403
    task = create(client, spec=f"Deploy. {SECRET}")
    ask(client, worker, f"Which key? {SECRET}")
    text = json.dumps(actionable(client))
    assert GH_TOKEN not in text and "abcdefghijklmnop" not in text
    a = row(json.loads(text), task["id"])["actionable"]
    assert "[redacted]" in a["question"]["text"]
    assert "[redacted]" in a["spec_excerpt"]["text"]


def test_mcp_returns_the_same_projection(client: TestClient) -> None:
    worker = register(client)
    create(client)
    ask(client, worker)
    rest = actionable(client)
    mcp = tool(client, ADMIN_TOKEN, "get_inbox", view="actionable")
    assert [r["actionable"] for r in mcp["items"]] == [
        r["actionable"] for r in rest["items"]
    ]
    assert tool(client, register(client), "get_inbox", view="actionable")["error"]


# -- delivery evidence and live forge reads -----------------------------------


def test_stale_stored_pr_conflicts_with_a_newer_merged_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_repository(client)
    tid = review_task(client, f"opened {URL}")
    store_pr(client, tid, "open")
    monkeypatch.setattr(inbox_actions, "STALE_S", -1)  # every stored value is old
    forge(client, pr_state("closed", merged=True))
    stored_only = row(actionable(client), tid)["actionable"]["delivery"]
    assert stored_only["stored"]["status"] == "stale"
    assert stored_only["stored"]["source"] == "stored_observation"
    assert stored_only["headline"]["value"] == "open"

    page = actionable(client, forge="true")
    d = row(page, tid)["actionable"]["delivery"]
    assert d["status"] == "conflict"
    assert d["stored"]["value"] == "open" and d["live"]["value"] == "merged"
    assert d["headline"] == d["live"] and d["headline"]["source"] == "live_forge"
    assert d["live"]["observed_at"] >= page["snapshot_at"]
    assert d["flags"] == ["delivery_merged_pending_review"]
    assert d["pr"] == URL
    a = row(page, tid)["actionable"]
    assert a["next"]["action"] == "answer_decision"  # a merge accepts nothing
    assert ok(client.get(f"/api/tasks/{tid}"))["state"] == "in_review"


def test_worker_claims_are_labelled_not_verified(client: TestClient) -> None:
    worker = register(client)
    task = create(client)
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    report = {
        "attempt": claimed["attempt"],
        "artifacts": [{"kind": "pr", "ref": "7", "state": "merged"}],
    }
    res = client.post(
        f"/api/tasks/{task['id']}/observations", json=report, headers=worker
    )
    assert res.status_code == 200, res.text
    client.post(
        f"/api/tasks/{task['id']}/block",
        json={"attempt": claimed["attempt"], "reason": "worker_env"},
        headers=worker,
    )
    d = row(actionable(client), task["id"])["actionable"]["delivery"]
    assert d["stored"]["value"] == "merged"
    assert d["stored"]["source"] == "worker_claim"  # what the worker said, unverified
    assert d["live"]["error"] == "not_requested"
    # Orchestrator-recorded WIP is a stored observation.
    revision = ok(client.get(f"/api/tasks/{task['id']}/continuation"))["revision"]
    ok(
        client.patch(
            f"/api/tasks/{task['id']}/continuation",
            json={
                "expected_revision": revision,
                "reason": "wip",
                "wip": {"summary": "half done"},
            },
        )
    )
    wip = row(actionable(client), task["id"])["actionable"]["wip"]
    assert (wip["value"], wip["source"], wip["status"]) == (
        "half done",
        "stored_observation",
        "fresh",
    )


def test_forge_unavailable_degrades_without_leaking(client: TestClient) -> None:
    setup_repository(client)
    tid = review_task(client, f"opened {URL}")
    forge(
        client,
        lambda _: httpx.Response(500, text=f"boom token={GH_TOKEN} dummy-token"),
    )
    page = actionable(client, forge="true")
    d = row(page, tid)["actionable"]["delivery"]
    assert d["live"]["status"] == "unknown"
    assert d["live"]["error"] == "unavailable"
    assert d["status"] == "unknown"
    text = json.dumps(page)
    assert "dummy-token" not in text and GH_TOKEN not in text and "boom" not in text
    assert "/api/v1/" not in text  # the request URL is not returned
    assert page["external"]["reads"] == 1 and page["external"]["timed_out"] == 0


def test_forge_timeout_is_bounded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_repository(client)
    tid = review_task(client, f"opened {URL}")

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={"state": "open", "merged": False})

    forge(client, slow)
    monkeypatch.setattr(inbox_actions, "FORGE_BUDGET_S", 0.05)
    page = actionable(client, forge="true")
    assert row(page, tid)["actionable"]["delivery"]["live"]["error"] == "timeout"
    assert page["external"]["timed_out"] == 1
    assert page["external"]["budget_s"] == 0.05


def test_forge_reads_are_capped_per_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_repository(client)
    monkeypatch.setattr(inbox_actions, "FORGE_READS", 1)
    first = review_task(client, f"opened {URL}")
    second = review_task(client, f"opened {URL.replace('/12', '/13')}")
    forge(client, pr_state("open"))
    page = actionable(client, forge="true")
    lives = [row(page, t)["actionable"]["delivery"]["live"] for t in (first, second)]
    assert lives[0]["value"] == "open" and lives[0]["status"] == "fresh"
    assert lives[1]["error"] == "read_budget"
    no_pr = review_task(client, "no link")
    assert (
        row(actionable(client, forge="true"), no_pr)["actionable"]["delivery"]["live"][
            "error"
        ]
        == "no_reference"
    )


def test_decision_answered_during_inspection_is_flagged(client: TestClient) -> None:
    setup_repository(client)
    tid = review_task(client, f"opened {URL}")
    decision_id = ok(client.get("/api/inbox"))["items"][0]["decision_id"]
    db = client.app.state.db  # ty: ignore[unresolved-attribute]

    async def answer_meanwhile(_: httpx.Request) -> httpx.Response:
        async with db.write() as tx:  # someone answers while the forge is read
            await tx.conn.execute(
                "UPDATE decisions SET state = 'answered', decided_at = ? WHERE id = ?",
                (ts(tx.now), decision_id),
            )
        return httpx.Response(200, json={"state": "open", "merged": False})

    forge(client, answer_meanwhile)
    page = actionable(client, forge="true")
    item = row(page, tid)
    # The snapshot still shows the open decision; the change is flagged, not hidden.
    assert item["decision_id"] == decision_id
    assert item["actionable"]["decision_state_changed"] is True
    assert item["actionable"]["next"]["action"] == "reread"


def test_external_reads_are_not_part_of_the_snapshot(client: TestClient) -> None:
    setup_repository(client)
    review_task(client, f"opened {URL}")
    forge(client, pr_state("open"))
    page = actionable(client, forge="true")
    ext = page["external"]
    assert ext["in_snapshot"] is False and ext["requested"] is True
    assert page["snapshot_at"] <= ext["started_at"] <= ext["finished_at"]
    live = page["items"][0]["actionable"]["delivery"]["live"]
    assert live["source"] == "live_forge"
    assert live["observed_at"] >= page["snapshot_at"]
