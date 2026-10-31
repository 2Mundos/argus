"""Routine persistence and management API (docs/specs/routines.spec.md 3.1, 3.4)."""

import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api.db import Database, ts
from argus_api.errors import ForbiddenError, ValidationFailedError
from argus_api.main import app
from argus_api.routines import service
from argus_api.tasks import service as tasks
from argus_core.routines import RoutineWrite
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from test_mcp import listed, tool

ADMIN_TOKEN = "test-admin-token-" + "q" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
ROUTINE = {
    "title": "Error log",
    "schedule": "@hourly",
    "template": {"spec": "Read the logs since the last run.", "priority": 2},
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        for slug in ("alpha", "beta"):
            c.put(f"/api/projects/{slug}", json={"name": slug}, headers=ADMIN)
        yield c


def register(client: TestClient, role: str = "worker") -> str:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201, res.text
    return res.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def put(client: TestClient, headers: dict, path: str = "alpha/error-log", **body):
    project, slug = path.split("/")
    return client.put(
        f"/api/projects/{project}/routines/{slug}",
        json=ROUTINE | body,
        headers=headers,
    )


def code(res) -> str:
    return res.json()["detail"]["code"]


# -- REST --------------------------------------------------------------------------


def test_put_creates_and_get_previews_three_slots(client: TestClient) -> None:
    orch = bearer(register(client, "orchestrator"))
    res = put(client, orch, timezone="Europe/Lisbon")
    assert res.status_code == 200, res.text
    routine = res.json()
    assert routine["project"] == "alpha"
    assert routine["slug"] == "error-log"
    assert routine["findings"] == "approval"
    assert routine["enabled"] is True
    assert routine["timezone"] == "Europe/Lisbon"
    assert routine["template"] == ROUTINE["template"]
    assert routine["created_by"].startswith("agent:")

    got = client.get(f"/api/routines/{routine['id']}", headers=orch).json()
    slots = [datetime.fromisoformat(s) for s in got["next_slots"]]
    assert len(slots) == 3
    assert slots == sorted(slots)
    assert all(s.minute == 0 and s.second == 0 for s in slots)
    assert got["next_run_at"] is not None
    assert got["last_runs"] == []

    listed_ = client.get("/api/projects/alpha/routines", headers=orch).json()
    assert [r["id"] for r in listed_] == [routine["id"]]
    assert len(listed_[0]["next_slots"]) == 3
    assert client.get("/api/projects/beta/routines", headers=orch).json() == []


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({"schedule": "61 * * * *"}, 422, "invalid_schedule"),
        ({"schedule": "@yearly"}, 422, "invalid_schedule"),
        ({"schedule": "0 0 31 2 *"}, 422, "invalid_schedule"),  # never fires
        ({"timezone": "Mars/Olympus"}, 422, "invalid_timezone"),
        ({"template": {"project": "beta"}}, 422, "unknown_template_field"),
        ({"template": {"depends_on": ["x"]}}, 422, "unknown_template_field"),
        ({"template": {"priority": "high"}}, 422, "validation_failed"),
        ({"template": {"title": ""}}, 422, "validation_failed"),
        ({"template": {"review": "maybe"}}, 422, "validation_failed"),
        ({"template": {"kind": "nope"}}, 404, "unknown_kind"),
        # The kind's input schema applies, as at task creation.
        ({"template": {"kind": "code_review", "input": {}}}, 422, "validation_failed"),
        ({"slug": "other"}, 422, "identity_mismatch"),
        ({"project": "beta"}, 422, "identity_mismatch"),
    ],
)
def test_put_rejects_invalid_routines(
    client: TestClient, body: dict, status: int, error: str
) -> None:
    res = put(client, ADMIN, **body)
    assert res.status_code == status, res.text
    assert code(res) == error
    assert client.get("/api/projects/alpha/routines", headers=ADMIN).json() == []


def test_put_rejects_bad_findings_slug_and_project(client: TestClient) -> None:
    assert put(client, ADMIN, findings="sometimes").status_code == 422
    assert put(client, ADMIN, unknown=1).status_code == 422
    assert put(client, ADMIN, "alpha/Error_Log").status_code == 422
    res = put(client, ADMIN, "nowhere/error-log")
    assert (res.status_code, code(res)) == (404, "unknown_project")


def test_template_keeps_only_given_fields_and_validates_pins(
    client: TestClient,
) -> None:
    template = {
        "title": "Hourly error scan",
        "kind": "generic",
        "acceptance": ["one task per new error"],
        "required_capabilities": ["logs"],
        "difficulty": "standard",
        "harness_model": {"engine": "codex", "model": "gpt-5"},
    }
    res = put(client, ADMIN, template=template)
    assert res.status_code == 200, res.text
    assert res.json()["template"] == template
    bad = put(
        client, ADMIN, template={"harness_model": {"engine": "nope", "model": "x"}}
    )
    assert bad.status_code == 422


def test_put_replaces_keeping_identity(client: TestClient) -> None:
    orch = bearer(register(client, "orchestrator"))
    first = put(client, orch).json()
    second = put(
        client,
        ADMIN,
        title="Errors",
        schedule="*/15 * * * *",
        enabled=False,
        slug="error-log",
        project="alpha",
    )
    assert second.status_code == 200, second.text
    after = second.json()
    assert after["id"] == first["id"]
    assert after["created_by"] == first["created_by"]
    assert after["created_at"] == first["created_at"]
    assert (after["title"], after["schedule"]) == ("Errors", "*/15 * * * *")
    assert after["enabled"] is False
    assert after["next_run_at"] is None
    assert len(after["next_slots"]) == 3  # the preview stays

    rows = client.get("/api/projects/alpha/routines", headers=ADMIN).json()
    assert len(rows) == 1
    events = client.get(
        "/api/events",
        params={"subject_type": "routine", "subject_id": first["id"]},
        headers=ADMIN,
    ).json()
    assert [e["kind"] for e in reversed(events)] == [
        "routine.created",
        "routine.updated",
        "routine.disabled",
    ]
    # Template text never enters the event log.
    assert "Read the logs" not in str(events)


def test_slug_is_unique_per_project_only(client: TestClient) -> None:
    a = put(client, ADMIN, "alpha/error-log").json()
    b = put(client, ADMIN, "beta/error-log").json()
    assert a["id"] != b["id"]
    assert put(client, ADMIN, "alpha/error-log").json()["id"] == a["id"]


def test_patch_toggles_enabled(client: TestClient) -> None:
    orch = bearer(register(client, "orchestrator"))
    routine = put(client, orch).json()
    path = "/api/projects/alpha/routines/error-log"
    off = client.patch(path, json={"enabled": False}, headers=orch)
    assert off.status_code == 200, off.text
    assert (off.json()["enabled"], off.json()["next_run_at"]) == (False, None)
    on = client.patch(path, json={"enabled": True}, headers=orch).json()
    assert on["enabled"] is True
    assert on["next_run_at"] is not None
    kinds = [
        e["kind"]
        for e in client.get(
            "/api/events",
            params={"subject_type": "routine", "subject_id": routine["id"]},
            headers=ADMIN,
        ).json()
    ]
    assert kinds[:2] == ["routine.updated", "routine.disabled"]
    missing = client.patch(
        "/api/projects/alpha/routines/nope", json={"enabled": False}, headers=orch
    )
    assert missing.status_code == 404


def test_delete_removes_the_routine(client: TestClient) -> None:
    orch = bearer(register(client, "orchestrator"))
    routine = put(client, orch).json()
    res = client.delete("/api/projects/alpha/routines/error-log", headers=orch)
    assert res.status_code == 204
    assert client.get(f"/api/routines/{routine['id']}", headers=orch).status_code == 404
    again = client.delete("/api/projects/alpha/routines/error-log", headers=orch)
    assert again.status_code == 404


def test_workers_read_leased_projects_and_never_write(client: TestClient) -> None:
    routine = put(client, ADMIN).json()
    put(client, ADMIN, "beta/error-log")
    worker = bearer(register(client))
    task = client.post(
        "/api/tasks", json={"title": "t", "project": "alpha"}, headers=ADMIN
    )
    assert task.status_code == 201, task.text
    claimed = client.post("/api/tasks/claim", json={}, headers=worker)
    assert claimed.status_code == 200, claimed.text

    assert client.get("/api/projects/alpha/routines", headers=worker).status_code == 200
    assert (
        client.get(f"/api/routines/{routine['id']}", headers=worker).status_code == 200
    )
    denied = client.get("/api/projects/beta/routines", headers=worker)
    assert (denied.status_code, code(denied)) == (403, "project_scope")

    path = "/api/projects/alpha/routines/error-log"
    assert put(client, worker).status_code == 403
    assert (
        client.patch(path, json={"enabled": False}, headers=worker).status_code == 403
    )
    assert client.delete(path, headers=worker).status_code == 403
    assert client.get("/api/projects/alpha/routines").status_code == 401


def test_auto_findings_are_admin_only(client: TestClient) -> None:
    orch = bearer(register(client, "orchestrator"))
    res = put(client, orch, findings="auto")
    assert (res.status_code, code(res)) == (403, "admin_required")

    assert put(client, ADMIN, findings="auto").status_code == 200
    # An orchestrator may not rewrite an auto routine, even out of auto.
    res = put(client, orch, findings="approval")
    assert (res.status_code, code(res)) == (403, "admin_required")
    # Disabling is always allowed; turning it back on is not.
    path = "/api/projects/alpha/routines/error-log"
    assert client.patch(path, json={"enabled": False}, headers=orch).status_code == 200
    res = client.patch(path, json={"enabled": True}, headers=orch)
    assert (res.status_code, code(res)) == (403, "admin_required")
    assert client.patch(path, json={"enabled": True}, headers=ADMIN).status_code == 200


# -- MCP ---------------------------------------------------------------------------


def test_mcp_tools_share_the_rest_rules(client: TestClient) -> None:
    worker, orch = register(client), register(client, "orchestrator")
    names = {"list_routines", "get_routine", "upsert_routine", "set_routine_enabled"}
    assert names <= listed(client, orch)
    assert names <= listed(client, ADMIN_TOKEN)
    assert {"list_routines", "get_routine"} <= listed(client, worker)
    assert not {"upsert_routine", "set_routine_enabled"} & listed(client, worker)
    assert "run_routine" not in listed(client, ADMIN_TOKEN)

    auto = tool(
        client,
        orch,
        "upsert_routine",
        project="alpha",
        slug="error-log",
        title="Error log",
        schedule="@daily",
        findings="auto",
    )
    assert "403 admin_required" in auto["error"]
    bad = tool(
        client,
        orch,
        "upsert_routine",
        project="alpha",
        slug="error-log",
        title="Error log",
        schedule="0 25 * * *",
    )
    assert "422 invalid_schedule" in bad["error"]

    made = tool(
        client,
        orch,
        "upsert_routine",
        project="alpha",
        slug="error-log",
        title="Error log",
        schedule="@daily",
        template={"spec": "scan"},
    )
    assert made["template"] == {"spec": "scan"}
    assert len(made["next_slots"]) == 3
    assert [r["id"] for r in tool(client, orch, "list_routines", project="alpha")] == [
        made["id"]
    ]
    assert tool(client, orch, "get_routine", routine_id=made["id"])["id"] == made["id"]
    assert "403" in tool(client, worker, "list_routines", project="alpha")["error"]
    denied = tool(
        client,
        worker,
        "set_routine_enabled",
        project="alpha",
        slug="error-log",
        enabled=False,
    )
    assert "403" in denied["error"]
    off = tool(
        client,
        orch,
        "set_routine_enabled",
        project="alpha",
        slug="error-log",
        enabled=False,
    )
    assert off["enabled"] is False

    admin_auto = tool(
        client,
        ADMIN_TOKEN,
        "upsert_routine",
        project="alpha",
        slug="error-log",
        title="Error log",
        schedule="@daily",
        findings="auto",
    )
    assert admin_auto["findings"] == "auto"


# -- service and schema ------------------------------------------------------------


async def _project(db: Database, slug: str = "radar") -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO projects (slug, name, description, created_at, updated_at)"
            " VALUES (?, ?, '', ?, ?)",
            (slug, slug, ts(tx.now), ts(tx.now)),
        )


async def test_next_run_at_comes_from_tx_now(db: Database, clock: FakeClock) -> None:
    await _project(db)
    clock.now = datetime(2026, 3, 28, 12, 30, tzinfo=UTC)
    body = RoutineWrite(title="Nightly", schedule="0 2 * * *", timezone="Europe/Lisbon")
    async with db.write() as tx:
        routine = await service.upsert(
            tx, "radar", "nightly", body, "admin:test", admin=True
        )
    # Lisbon moves to summer time on 2026-03-29: 02:00 local is 01:00 UTC from then.
    assert routine.next_run_at == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert routine.next_slots == [
        datetime(2026, 3, 29, 1, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 1, 0, tzinfo=UTC),
        datetime(2026, 3, 31, 1, 0, tzinfo=UTC),
    ]


async def test_replacement_recomputes_next_run(db: Database, clock: FakeClock) -> None:
    await _project(db)
    body = RoutineWrite(title="r", schedule="@daily")
    async with db.write() as tx:
        first = await service.upsert(tx, "radar", "r", body, "agent:a", admin=False)
    clock.advance(3 * 86400)
    async with db.write() as tx:
        second = await service.upsert(tx, "radar", "r", body, "agent:b", admin=False)
    assert first.next_run_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert second.next_run_at == datetime(2026, 1, 5, tzinfo=UTC)
    assert (second.id, second.created_by, second.created_at) == (
        first.id,
        "agent:a",
        first.created_at,
    )


async def test_service_rejects_auto_for_non_admins(db: Database) -> None:
    await _project(db)
    body = RoutineWrite(title="r", schedule="@daily", findings="auto")
    with pytest.raises(ForbiddenError):
        async with db.write() as tx:
            await service.upsert(tx, "radar", "r", body, "agent:a", admin=False)
    with pytest.raises(ValidationFailedError):
        async with db.write() as tx:
            await service.upsert(
                tx,
                "radar",
                "r",
                RoutineWrite(title="r", schedule="@daily", template={"x": 1}),
                "agent:a",
                admin=False,
            )


async def test_delete_keeps_run_tasks(db: Database) -> None:
    await _project(db)
    async with db.write() as tx:
        routine = await service.upsert(
            tx,
            "radar",
            "r",
            RoutineWrite(title="r", schedule="@hourly"),
            "admin:test",
            admin=True,
        )
        run = await tasks.create_task(
            tx, TaskCreate(title="run", project="radar"), f"routine:{routine.id}"
        )
        await tx.conn.execute(
            "UPDATE routines SET last_task_id = ? WHERE id = ?", (run.id, routine.id)
        )
    async with db.read() as conn:
        got = await service.get_routine(conn, routine.id, db.now())
    assert [r.task_id for r in got.last_runs] == [run.id]
    assert got.last_task_id == run.id

    async with db.write() as tx:
        await service.delete(tx, "radar", "r", "admin:test")
    async with db.read() as conn:
        assert (await tasks.get_task(conn, run.id)).created_by == (
            f"routine:{routine.id}"
        )
        async with conn.execute("SELECT count(*) FROM routines") as cur:
            assert (await cur.fetchone())[0] == 0


async def test_schema_has_due_index_and_constraints(db: Database) -> None:
    await _project(db)
    async with db.read() as conn:
        async with conn.execute("PRAGMA index_list(routines)") as cur:
            indexes = {r["name"]: r for r in await cur.fetchall()}
        async with conn.execute("PRAGMA index_info(routines_due)") as cur:
            columns = [r["name"] for r in await cur.fetchall()]
    assert columns == ["enabled", "next_run_at"]
    assert any(r["unique"] for n, r in indexes.items() if n != "routines_due")

    insert = (
        "INSERT INTO routines (id, project, slug, title, schedule, template,"
        " findings, created_by, created_at, updated_at)"
        " VALUES (?, ?, 's', 't', '@daily', ?, ?, 'x', 'x', 'x')"
    )
    async with db.write() as tx:
        await tx.conn.execute(insert, ("1", "radar", "{}", "off"))
        async with tx.conn.execute(
            "SELECT timezone, enabled FROM routines WHERE id = '1'"
        ) as cur:
            assert tuple(await cur.fetchone()) == ("UTC", 1)
    for values in (
        ("2", "radar", "{}", "off"),  # duplicate (project, slug)
        ("3", "radar2", "{}", "off"),  # unknown project
        ("4", "global", "not json", "off"),
        ("5", "global", "{}", "always"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            async with db.write() as tx:
                await tx.conn.execute(insert, values)
