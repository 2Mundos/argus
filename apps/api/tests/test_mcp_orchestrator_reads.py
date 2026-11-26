"""Queue, task recovery, routine template patch and the summary views (MCP operations
3.2, 3.6; routines 3.4)."""

import hashlib
import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient
from test_list_reads import ask, create, ok, register
from test_mcp import tool

ADMIN_TOKEN = "test-admin-token-" + "m" * 32  # test_mcp's, so `tool` works too
ROUTINE = {
    "title": "Error log",
    "schedule": "@hourly",
    "template": {"spec": "Read the logs.", "priority": 2, "title": "Logs"},
}


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    root = tmp_path / "archives"
    root.mkdir()
    return root


@pytest.fixture
def client(
    tmp_path: Path, archive_root: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_WORKSPACE_CLEANUP_ARCHIVE_ROOT", str(archive_root))
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        for slug in ("alpha", "beta"):
            c.put(f"/api/projects/{slug}", json={"name": slug})
        yield c


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "argus.db"


# -- get_queue ---------------------------------------------------------------------


def test_queue_lists_live_work_only(client: TestClient) -> None:
    worker = register(client)
    asked = create(client, title="asks " + "q" * 150)
    decision = ask(client, worker, "Which key? " + "x" * 400)
    pending = create(client, depends_on=[asked["id"]], title="pending")
    done = create(client, title="finished")
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == done["id"]
    res = client.post(
        f"/api/tasks/{done['id']}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": "ok"}},
        headers=worker,
    )
    assert res.status_code == 200, res.text
    ready = create(client, "beta", title="stays ready")

    queue = ok(client.get("/api/tasks/queue"))
    ids = {t["id"] for t in queue["items"]}
    assert ids == {ready["id"], asked["id"]}  # not the pending or the finished one
    assert pending["id"] not in ids and done["id"] not in ids
    row = next(t for t in queue["items"] if t["id"] == asked["id"])
    assert len(row["id"]) == 36 and row["project"] == "alpha"
    assert row["decision"]["id"] == decision["id"] and len(decision["id"]) == 36
    assert row["decision"]["kind"] and len(row["decision"]["question"]) <= 300
    assert {"attempt", "failures", "max_attempts", "block_reason", "title"} <= set(row)
    assert queue["totals_by_state"]["blocked"] + queue["totals_by_state"]["ready"] >= 2
    assert queue["truncated"] is False

    only = ok(client.get("/api/tasks/queue", params={"project": "beta"}))
    assert [t["id"] for t in only["items"]] == [ready["id"]]
    states = ok(client.get("/api/tasks/queue", params={"state": "ready"}))
    assert {t["state"] for t in states["items"]} == {"ready"}
    assert client.get("/api/tasks/queue", params={"state": "done"}).status_code == 422
    assert client.get("/api/tasks/queue", headers=register(client)).status_code == 403


def test_fifty_active_tasks_stay_under_20_kb(client: TestClient) -> None:
    for i in range(50):
        create(client, title=f"task {i} " + "t" * 150, spec="s" * 5000)
    res = client.get("/api/tasks/queue")
    assert len(ok(res)["items"]) == 50 and len(res.content) < 20_000


def test_queue_tool_matches_rest(client: TestClient) -> None:
    create(client)
    out = tool(client, ADMIN_TOKEN, "get_queue", states=["ready"])
    assert out["totals_by_state"]["ready"] == 1 and len(out["items"]) == 1


# -- get_task_recovery -------------------------------------------------------------


def claim_as_instance(client: TestClient, tmp_path: Path, task: dict, instance: str):
    worker = register(client)
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert claimed["id"] == task["id"]
    with sqlite3.connect(db_path(tmp_path)) as db:
        agent_id = db.execute(
            "SELECT agent_id FROM task_attempts WHERE task_id = ?", (task["id"],)
        ).fetchone()[0]
        db.execute(
            "UPDATE agents SET instance_id = ? WHERE id = ?", (instance, agent_id)
        )
    return agent_id, worker


def ledger(tmp_path: Path, instance: str, status: str, reason: str, path: str | None):
    with sqlite3.connect(db_path(tmp_path)) as db:
        db.execute(
            "INSERT INTO workspace_cleanup (root, name, instance_id, status, reason,"
            " archive_path, last_run_id, first_seen_at, updated_at)"
            " VALUES ('/r', ?, ?, ?, ?, ?, 'run', '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z')",
            (instance + "-dir", instance, status, reason, path),
        )


def write_archive(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir()
    body = b"diff --git a b\n"
    (directory / "wip.patch").write_bytes(body)
    manifest = {
        "repos": [
            {
                "rel": "repo",
                "branch": "argus/task-1",
                "head": "abc123",
                "local_commits": 2,
                "has_wip": True,
                "staged_count": 1,
                "unstaged_count": 3,
                "untracked_count": 4,
                "artifacts": [
                    {
                        "name": "wip.patch",
                        "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                ],
            }
        ]
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_recovery_of_an_archived_and_a_cleaned_without_archive_instance(
    client: TestClient, tmp_path: Path, archive_root: Path
) -> None:
    task = create(client)
    agent_id, worker = claim_as_instance(client, tmp_path, task, "inst-1")
    directory = write_archive(archive_root, "inst-1")
    ledger(tmp_path, "inst-1", "cleaned", "idle", str(directory))
    up = client.post(
        f"/api/tasks/{task['id']}/artifacts",
        params={"attempt": 1, "name": "report.md"},
        content=b"# hi",
        headers={**worker, "Content-Type": "text/markdown"},
    )
    assert up.status_code == 201, up.text

    out = ok(client.get(f"/api/tasks/{task['id']}/recovery"))
    (attempt,) = out["attempts"]
    assert (attempt["agent_id"], attempt["instance_id"]) == (agent_id, "inst-1")
    (row,) = attempt["ledger"]
    assert (row["status"], row["reason"], row["archive_path"]) == (
        "cleaned",
        "idle",
        str(directory),
    )
    archive = row["archive"]
    assert archive["verified"] is True
    (repo,) = archive["repos"]
    assert (repo["branch"], repo["head"], repo["local_commits"]) == (
        "argus/task-1",
        "abc123",
        2,
    )
    assert (repo["has_wip"], repo["staged_count"]) == (True, 1)
    assert (repo["unstaged_count"], repo["untracked_count"]) == (3, 4)
    (file,) = repo["artifacts"]
    assert file["name"] == "wip.patch" and file["bytes"] == 15
    assert file["sha256"] == hashlib.sha256(b"diff --git a b\n").hexdigest()
    assert [a["name"] for a in out["artifacts"]] == ["report.md"]
    assert out["artifacts"][0]["sha256"] == hashlib.sha256(b"# hi").hexdigest()
    assert "diff --git" not in json.dumps(out)  # metadata only

    (directory / "wip.patch").write_bytes(b"tampered contents")
    tampered = ok(client.get(f"/api/tasks/{task['id']}/recovery"))
    archive = tampered["attempts"][0]["ledger"][0]["archive"]
    assert archive["verified"] is False

    with sqlite3.connect(db_path(tmp_path)) as db:
        db.execute("UPDATE workspace_cleanup SET archive_path = NULL")
    bare = ok(client.get(f"/api/tasks/{task['id']}/recovery"))
    (row,) = bare["attempts"][0]["ledger"]
    assert row["status"] == "cleaned" and row["archive"] is None


def test_recovery_reads_only_under_the_archive_root(
    client: TestClient, tmp_path: Path
) -> None:
    task = create(client)
    claim_as_instance(client, tmp_path, task, "inst-2")
    outside = write_archive(tmp_path, "elsewhere")
    ledger(tmp_path, "inst-2", "cleaned", "idle", str(outside))
    out = ok(client.get(f"/api/tasks/{task['id']}/recovery"))
    archive = out["attempts"][0]["ledger"][0]["archive"]
    assert archive["verified"] is False and archive["problem"] == "outside_archive_root"
    assert archive["repos"] == []


def test_recovery_of_a_task_without_attempts(client: TestClient) -> None:
    task = create(client)
    out = ok(client.get(f"/api/tasks/{task['id']}/recovery"))
    assert out == {"task_id": task["id"], "attempts": [], "artifacts": []}
    assert client.get("/api/tasks/nope/recovery").status_code == 404
    assert (
        client.get(f"/api/tasks/{task['id']}/recovery", headers=register(client))
    ).status_code == 403
    assert tool(client, ADMIN_TOKEN, "get_task_recovery", task_id=task["id"]) == out


# -- patch_routine_template --------------------------------------------------------


def put_routine(client: TestClient, slug: str, project: str = "alpha", **tpl) -> dict:
    body = {**ROUTINE, "template": {**ROUTINE["template"], **tpl}}
    res = client.put(f"/api/projects/{project}/routines/{slug}", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def stored_templates(tmp_path: Path) -> dict[str, str]:
    with sqlite3.connect(db_path(tmp_path)) as db:
        return dict(db.execute("SELECT id, template FROM routines").fetchall())


def patch(client: TestClient, project: str = "alpha", **body):
    return client.patch(f"/api/projects/{project}/routines", json=body)


def test_patch_changes_only_named_fields(client: TestClient, tmp_path: Path) -> None:
    one = put_routine(client, "one")
    two = put_routine(client, "two", spec="Other spec.")
    other = put_routine(client, "three", project="beta")
    before = {k: json.loads(v) for k, v in stored_templates(tmp_path).items()}

    res = patch(client, routines="all", set={"priority": 1}, append_to_spec="Be brief.")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["dry_run"] is False and len(out["routines"]) == 2
    first = next(r for r in out["routines"] if r["slug"] == "one")
    assert first["spec_len_after"] - first["spec_len_before"] == len("Be brief.")
    assert {c["field"] for c in first["changes"]} == {"priority", "spec"}

    after = {k: json.loads(v) for k, v in stored_templates(tmp_path).items()}
    for routine in (one, two):
        old, new = before[routine["id"]], after[routine["id"]]
        assert new["priority"] == 1 and new["spec"].endswith("Be brief.")
        assert {k: v for k, v in new.items() if k not in ("priority", "spec")} == {
            k: v for k, v in old.items() if k not in ("priority", "spec")
        }
    assert after[other["id"]] == before[other["id"]]  # another project

    events = client.get("/api/events", params={"kind": "routine.updated"}).json()
    items = events["items"] if isinstance(events, dict) else events
    touched = {e["subject_id"] for e in items}
    assert {one["id"], two["id"]} <= touched


def test_patch_dry_run_writes_nothing(client: TestClient, tmp_path: Path) -> None:
    put_routine(client, "one")
    before = stored_templates(tmp_path)
    res = patch(client, routines=["one"], set={"priority": 3}, dry_run=True)
    assert res.status_code == 200, res.text
    assert res.json()["dry_run"] is True
    assert res.json()["routines"][0]["changes"][0]["field"] == "priority"
    assert stored_templates(tmp_path) == before


def test_patch_rejects_unknown_fields_and_changes_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    put_routine(client, "one")
    put_routine(client, "two")
    before = stored_templates(tmp_path)
    res = patch(client, routines="all", set={"priority": 1, "colour": "red"})
    assert res.status_code == 422, res.text
    res = patch(client, routines=["one", "missing"], set={"priority": 1})
    assert res.status_code == 404, res.text
    assert patch(client, routines="all").status_code == 422  # nothing to change
    assert stored_templates(tmp_path) == before


def test_patch_refuses_a_stale_version(client: TestClient) -> None:
    routine = put_routine(client, "one")
    version = ok(client.get("/api/projects/alpha/routines/one"))["version"]
    assert patch(
        client, routines=["one"], set={"priority": 1}, expected_version=version
    )
    stale = patch(
        client, routines=["one"], set={"priority": 2}, expected_version="2020-01-01"
    )
    assert stale.status_code == 409, stale.text
    assert routine["id"]


def test_patch_is_admin_only_and_keeps_the_grant_rule(client: TestClient) -> None:
    put_routine(client, "one")
    orch = register(client, "orchestrator")
    res = client.patch(
        "/api/projects/alpha/routines",
        json={"routines": ["one"], "set": {"priority": 1}},
        headers=orch,
    )
    assert res.status_code == 403
    out = tool(
        client,
        ADMIN_TOKEN,
        "patch_routine_template",
        project="alpha",
        routines=["one"],
        set={"priority": 4},
        dry_run=True,
    )
    assert out["dry_run"] is True


# -- routine read by slug ----------------------------------------------------------


def test_routine_read_by_slug_works_whenever_the_list_returns_it(
    client: TestClient,
) -> None:
    created = put_routine(client, "error-log")
    listed = ok(client.get("/api/projects/alpha/routines"))
    assert [r["slug"] for r in listed] == ["error-log"]
    one = ok(client.get("/api/projects/alpha/routines/error-log"))
    assert one["id"] == created["id"] and one["slug"] == "error-log"
    assert client.get("/api/projects/alpha/routines/nope").status_code == 404
    assert client.get("/api/projects/beta/routines/error-log").status_code == 404
    via_tool = tool(
        client, ADMIN_TOKEN, "get_routine", routine_id="error-log", project="alpha"
    )
    assert via_tool["id"] == created["id"]


# -- view=summary ------------------------------------------------------------------


def test_summary_view_is_bounded_and_drops_models(client: TestClient) -> None:
    worker = register(client)
    ids = [create(client, title=f"t{i}", spec="s" * 4000)["id"] for i in range(20)]
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    client.post(
        f"/api/tasks/{claimed['id']}/progress",
        json={"attempt": claimed["attempt"], "message": "p" * 900},
        headers=worker,
    )
    res = client.get("/api/tasks", params={"ids": ",".join(ids), "view": "summary"})
    items = ok(res)["items"]
    assert len(items) == 20 and len(res.content) < 15_000
    assert all("models" not in t for t in items)
    assert all(len(t.get("progress_message") or "") <= 300 for t in items)
    assert tool(client, ADMIN_TOKEN, "get_tasks", ids=ids)["items"] == items
    detail = ok(client.get("/api/tasks", params={"ids": ids[0], "view": "detail"}))
    assert detail["items"][0]["spec"] and "models" in detail["items"][0]


def test_inbox_summary_carries_a_short_question(client: TestClient) -> None:
    worker = register(client)
    create(client)
    ask(client, worker, "Which? " + "x" * 600)
    page = ok(client.get("/api/inbox", params={"view": "summary"}))
    (item,) = page["items"]
    assert item["question"].startswith("Which?") and len(item["question"]) <= 300
    assert "models" not in json.dumps(item)
