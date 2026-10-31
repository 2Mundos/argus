"""Routine runs: scheduler, run now, pause and failure threshold
(docs/specs/routines.spec.md 3.2, 3.4 and 3.5)."""

import asyncio
import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.auth import Identity
from argus_api.db import Database, ts
from argus_api.decisions import service as decisions
from argus_api.errors import NotFoundError
from argus_api.inbox import inbox
from argus_api.main import app
from argus_api.routines import runs, service
from argus_api.settings import service as settings
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.decisions import DecisionAnswer
from argus_core.routines import Routine, RoutineWrite
from argus_core.tasks import PromptTrust, Task, TaskState
from fastapi.testclient import TestClient
from test_mcp import tool

PATH = Path("unused-model-policy.json")  # only the `models` setting reads it
START = datetime(2026, 1, 1, tzinfo=UTC)


async def _project(db: Database, slug: str = "radar") -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO projects (slug, name, description, created_at, updated_at)"
            " VALUES (?, ?, '', ?, ?)",
            (slug, slug, ts(tx.now), ts(tx.now)),
        )


async def _routine(
    db: Database,
    slug: str = "error-log",
    schedule: str = "@hourly",
    timezone: str = "UTC",
    template: dict | None = None,
    enabled: bool = True,
) -> Routine:
    body = RoutineWrite(
        title="Error log",
        schedule=schedule,
        timezone=timezone,
        template=template or {},
        enabled=enabled,
    )
    async with db.write() as tx:
        return await service.upsert(tx, "radar", slug, body, "admin:test", admin=True)


async def _tick(db: Database) -> list[str]:
    async with db.write() as tx:
        return await runs.tick(tx, PATH)


async def _run_now(db: Database, routine_id: str):
    async with db.write() as tx:
        return await runs.run_now(tx, routine_id, "admin:test")


async def _get(db: Database, routine_id: str) -> Routine:
    async with db.read() as conn:
        return await service.get_routine(conn, routine_id, db.now())


async def _task(db: Database, task_id: str) -> Task:
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def _events(db: Database, routine_id: str) -> list[tuple[str, str, dict]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT actor, kind, data FROM events WHERE subject_type = 'routine'"
            " AND subject_id = ? AND kind NOT IN ('routine.created', 'routine.updated')"
            " ORDER BY id",
            (routine_id,),
        ) as cur,
    ):
        return [(r[0], r[1], json.loads(r[2])) for r in await cur.fetchall()]


async def _pause(db: Database, enabled: bool) -> None:
    async with db.write() as tx:
        await settings.put_setting(
            tx, PATH, settings.GLOBAL, runs.ENABLED_KEY, enabled, "admin:test"
        )


async def _finish(db: Database, task_id: str, *, ok: bool) -> None:
    """Claim the (only ready) run and complete it, or fail its last attempt."""
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET max_attempts = 1 WHERE id = ?", (task_id,)
        )
    agent = f"a-{task_id[:8]}"
    await insert_agent(db, agent)
    async with db.write() as tx:
        claimed = await tasks.claim(tx, agent, [])
        assert claimed is not None and claimed.id == task_id
        if ok:
            await tasks.complete(tx, agent, task_id, claimed.attempt, DONE)
        else:
            await tasks.fail(tx, agent, task_id, claimed.attempt, "boom")
    assert (await _task(db, task_id)).state is (
        TaskState.SUCCEEDED if ok else TaskState.FAILED
    )


# -- scheduler -----------------------------------------------------------------------


async def test_due_routine_creates_one_run_task(db: Database, clock: FakeClock) -> None:
    await _project(db)
    template = {"spec": "Read the logs.", "priority": 2, "input": {"source": "prod"}}
    routine = await _routine(db, template=template)
    assert routine.next_run_at == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert await _tick(db) == []  # not due yet

    clock.now = datetime(2026, 1, 1, 1, 0, 20, tzinfo=UTC)
    [task_id] = await _tick(db)
    task = await _task(db, task_id)
    assert task.created_by == f"routine:{routine.id}"
    assert task.prompt_trust is PromptTrust.ORCHESTRATOR
    assert task.title == "Error log (2026-01-01 01:00 UTC)"
    assert (task.project, task.spec, task.priority) == ("radar", "Read the logs.", 2)
    assert task.state is TaskState.READY
    assert task.input == {
        "source": "prod",
        "routine": {
            "id": routine.id,
            "slug": "error-log",
            "run_at": ts(clock.now),
            "previous_run_at": None,
        },
    }
    got = await _get(db, routine.id)
    assert got.next_run_at == datetime(2026, 1, 1, 2, tzinfo=UTC)
    assert (got.last_run_at, got.last_task_id) == (clock.now, task_id)
    assert [r.task_id for r in got.last_runs] == [task_id]
    assert await _events(db, routine.id) == [
        ("system", "routine.run", {"task_id": task_id, "trigger": "schedule"})
    ]
    assert await _tick(db) == []  # already moved to the next slot


async def test_racing_ticks_create_a_single_run(db: Database, clock: FakeClock) -> None:
    await _project(db)
    routine = await _routine(db)
    clock.now = datetime(2026, 1, 1, 1, tzinfo=UTC)
    first, second = await asyncio.gather(_tick(db), _tick(db))
    assert sorted([len(first), len(second)]) == [0, 1]
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT count(*) FROM tasks WHERE created_by = ?",
            (f"routine:{routine.id}",),
        ) as cur,
    ):
        assert (await cur.fetchone())[0] == 1


async def test_downtime_gives_exactly_one_catch_up_run(
    db: Database, clock: FakeClock, tmp_path: Path
) -> None:
    await _project(db)
    routine = await _routine(db)
    clock.now = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)  # ten slots missed
    # A restart: the same database file, opened again.
    path = tmp_path / "argus.db"
    await db.close()
    reopened = await Database.open(path, readers=2, clock=clock)
    try:
        assert len(await _tick(reopened)) == 1
        assert await _tick(reopened) == []
        got = await _get(reopened, routine.id)
        assert got.next_run_at == datetime(2026, 1, 1, 11, tzinfo=UTC)
    finally:
        await reopened.close()


async def test_overlap_skips_a_scheduled_run_and_still_advances(
    db: Database, clock: FakeClock
) -> None:
    await _project(db)
    routine = await _routine(db)
    clock.now = datetime(2026, 1, 1, 1, tzinfo=UTC)
    [first] = await _tick(db)
    clock.now = datetime(2026, 1, 1, 2, tzinfo=UTC)
    assert await _tick(db) == []
    got = await _get(db, routine.id)
    assert got.next_run_at == datetime(2026, 1, 1, 3, tzinfo=UTC)
    assert got.last_task_id == first
    assert (await _events(db, routine.id))[-1] == (
        "system",
        "routine.skipped",
        {
            "reason": "previous_running",
            "previous_task_id": first,
            "trigger": "schedule",
        },
    )

    await _finish(db, first, ok=True)
    clock.now = datetime(2026, 1, 1, 3, tzinfo=UTC)
    [second] = await _tick(db)
    previous = (await _task(db, second)).input["routine"]["previous_run_at"]
    assert previous == ts(datetime(2026, 1, 1, 1, tzinfo=UTC))


async def test_template_that_no_longer_creates_is_skipped_alone(
    db: Database, clock: FakeClock
) -> None:
    await _project(db)
    broken = await _routine(db, slug="broken")
    healthy = await _routine(db, slug="healthy")
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE routines SET template = ? WHERE id = ?",
            (json.dumps({"kind": "gone"}), broken.id),
        )
    clock.now = datetime(2026, 1, 1, 1, tzinfo=UTC)
    [task_id] = await _tick(db)
    assert (await _task(db, task_id)).created_by == f"routine:{healthy.id}"
    _, kind, data = (await _events(db, broken.id))[-1]
    assert (kind, data["reason"], data["trigger"]) == (
        "routine.skipped",
        "create_failed",
        "schedule",
    )
    assert (await _get(db, broken.id)).next_run_at == datetime(
        2026, 1, 1, 2, tzinfo=UTC
    )
    with pytest.raises(NotFoundError):
        await _run_now(db, broken.id)


async def test_long_title_is_cut_to_fit_the_suffix(
    db: Database, clock: FakeClock
) -> None:
    await _project(db)
    await _routine(db, template={"title": "x" * 200})
    clock.now = datetime(2026, 1, 1, 1, tzinfo=UTC)
    [task_id] = await _tick(db)
    title = (await _task(db, task_id)).title
    assert len(title) == 200
    assert title.endswith("x (2026-01-01 01:00 UTC)")


# -- timezones -----------------------------------------------------------------------


async def test_runs_follow_the_routine_timezone(db: Database, clock: FakeClock) -> None:
    await _project(db)
    routine = await _routine(db, schedule="0 9 * * *", timezone="America/Sao_Paulo")
    # 09:00 in Sao Paulo (UTC-3, no DST since 2019) is 12:00 UTC.
    assert routine.next_run_at == datetime(2026, 1, 1, 12, tzinfo=UTC)
    clock.now = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
    [task_id] = await _tick(db)
    assert (await _task(db, task_id)).title == (
        "Error log (2026-01-01 09:00 America/Sao_Paulo)"
    )
    assert (await _get(db, routine.id)).next_run_at == datetime(
        2026, 1, 2, 12, tzinfo=UTC
    )


async def test_next_run_crosses_a_dst_change(db: Database, clock: FakeClock) -> None:
    await _project(db)
    clock.now = datetime(2026, 3, 7, tzinfo=UTC)
    routine = await _routine(db, schedule="0 9 * * *", timezone="America/New_York")
    assert routine.next_run_at == datetime(2026, 3, 7, 14, tzinfo=UTC)  # EST
    clock.now = datetime(2026, 3, 7, 14, tzinfo=UTC)
    [task_id] = await _tick(db)
    assert (await _task(db, task_id)).title == (
        "Error log (2026-03-07 09:00 America/New_York)"
    )
    # New York springs forward on 2026-03-08: 09:00 EDT is 13:00 UTC.
    assert (await _get(db, routine.id)).next_run_at == datetime(
        2026, 3, 8, 13, tzinfo=UTC
    )


# -- pause and disable ---------------------------------------------------------------


async def test_paused_routines_run_nothing_and_resume_with_one_catch_up(
    db: Database, clock: FakeClock
) -> None:
    await _project(db)
    routine = await _routine(db)
    await _pause(db, False)
    clock.now = datetime(2026, 1, 1, 5, tzinfo=UTC)
    assert await _tick(db) == []
    got = await _get(db, routine.id)
    assert (got.next_run_at, got.last_task_id) == (
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        None,
    )
    assert await _events(db, routine.id) == []

    manual = await _run_now(db, routine.id)  # run now still works while paused
    assert manual.task_id is not None
    await _finish(db, manual.task_id, ok=True)

    await _pause(db, True)
    assert len(await _tick(db)) == 1
    assert await _tick(db) == []
    got = await _get(db, routine.id)
    assert got.next_run_at == datetime(2026, 1, 1, 6, tzinfo=UTC)


async def test_disabled_routine_is_not_scheduled_but_can_run_now(
    db: Database, clock: FakeClock
) -> None:
    await _project(db)
    routine = await _routine(db, enabled=False)
    clock.now = datetime(2026, 1, 1, 5, tzinfo=UTC)
    assert await _tick(db) == []
    result = await _run_now(db, routine.id)
    assert result.task_id is not None and result.skipped is None
    got = await _get(db, routine.id)
    assert (got.enabled, got.next_run_at, got.last_task_id) == (
        False,
        None,
        result.task_id,
    )


# -- run now -------------------------------------------------------------------------


async def test_run_now_obeys_the_overlap_rule(db: Database, clock: FakeClock) -> None:
    await _project(db)
    routine = await _routine(db)
    clock.advance(600)
    first = await _run_now(db, routine.id)
    assert first.task_id is not None
    task = await _task(db, first.task_id)
    assert task.created_by == f"routine:{routine.id}"
    assert task.title == "Error log (2026-01-01 00:10 UTC)"

    second = await _run_now(db, routine.id)
    assert (second.task_id, second.skipped) == (None, "previous_running")
    got = await _get(db, routine.id)
    assert got.next_run_at == datetime(2026, 1, 1, 1, tzinfo=UTC)  # untouched
    assert got.last_task_id == first.task_id
    assert await _events(db, routine.id) == [
        ("admin:test", "routine.run", {"task_id": first.task_id, "trigger": "manual"}),
        (
            "admin:test",
            "routine.skipped",
            {
                "reason": "previous_running",
                "previous_task_id": first.task_id,
                "trigger": "manual",
            },
        ),
    ]
    # The scheduled slot is skipped too while the manual run is open.
    clock.now = datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert await _tick(db) == []

    async with db.write() as tx:
        await tasks.cancel(tx, first.task_id, "admin:test")
    third = await _run_now(db, routine.id)
    assert third.task_id is not None


async def test_run_now_of_an_unknown_routine(db: Database) -> None:
    with pytest.raises(NotFoundError):
        await _run_now(db, "nope")


# -- failure threshold ---------------------------------------------------------------


async def _scheduled_run(db: Database, clock: FakeClock) -> str:
    clock.advance(3600)
    [task_id] = await _tick(db)
    return task_id


async def test_three_consecutive_failures_disable_the_routine(
    db: Database, clock: FakeClock
) -> None:
    await _project(db)
    routine = await _routine(db)
    # fail, fail, success (resets), fail, fail: still enabled.
    for ok in (False, False, True, False, False):
        await _finish(db, await _scheduled_run(db, clock), ok=ok)
    assert (await _get(db, routine.id)).enabled is True

    last = await _scheduled_run(db, clock)
    await _finish(db, last, ok=False)
    got = await _get(db, routine.id)
    assert (got.enabled, got.next_run_at) == (False, None)
    _, kind, data = (await _events(db, routine.id))[-1]
    assert (kind, data["reason"]) == ("routine.disabled", "consecutive_failures")
    assert data["task_ids"][0] == last and len(data["task_ids"]) == 3
    clock.advance(3600)
    assert await _tick(db) == []

    async with db.snapshot() as snap:
        [item] = [i for i in (await inbox(snap)).items if i.subject_type == "routine"]
    assert (item.subject_id, item.title, item.project) == (
        routine.id,
        "Error log",
        "radar",
    )
    assert (item.decision_kind, item.group) == ("policy", "routine:radar")

    admin = Identity(token_id="t", scope=Scope.ADMIN, agent_id=None, label="test")
    async with db.write() as tx:
        await decisions.answer(
            tx, item.decision_id, DecisionAnswer(choice="approve"), admin
        )
    got = await _get(db, routine.id)
    assert got.enabled is True and got.next_run_at is not None
    # Re-enabling starts the count afresh: one more failure does not disable it.
    await _finish(db, await _scheduled_run(db, clock), ok=False)
    assert (await _get(db, routine.id)).enabled is True


async def test_enabling_by_hand_closes_the_inbox_question(
    db: Database, clock: FakeClock
) -> None:
    await _project(db)
    routine = await _routine(db)
    for _ in range(3):
        await _finish(db, await _scheduled_run(db, clock), ok=False)
    assert (await _get(db, routine.id)).enabled is False
    async with db.write() as tx:
        await service.set_enabled(
            tx, "radar", "error-log", True, "admin:test", admin=True
        )
    async with db.snapshot() as snap:
        assert not [i for i in (await inbox(snap)).items if i.subject_type == "routine"]


# -- REST and MCP --------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "r" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        c.put("/api/projects/alpha", json={"name": "alpha"}, headers=ADMIN)
        yield c


def _register(client: TestClient, role: str) -> str:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return res.json()["token"]


def test_run_now_route_permissions_and_skip(client: TestClient) -> None:
    routine = client.put(
        "/api/projects/alpha/routines/error-log",
        json={"title": "Error log", "schedule": "@daily", "enabled": False},
        headers=ADMIN,
    ).json()
    path = f"/api/routines/{routine['id']}/run"
    worker = _register(client, "worker")
    assert client.post(path).status_code == 401
    res = client.post(path, headers={"Authorization": f"Bearer {worker}"})
    assert res.status_code == 403
    assert client.post("/api/routines/nope/run", headers=ADMIN).status_code == 404

    orch = _register(client, "orchestrator")
    res = client.post(path, headers={"Authorization": f"Bearer {orch}"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["routine_id"] == routine["id"] and body["skipped"] is None
    task = client.get(f"/api/tasks/{body['task_id']}", headers=ADMIN).json()
    assert task["created_by"] == f"routine:{routine['id']}"
    assert task["prompt_trust"] == "orchestrator"

    again = client.post(path, headers=ADMIN).json()
    assert again == {
        "routine_id": routine["id"],
        "task_id": None,
        "skipped": "previous_running",
    }

    assert (
        "403" in tool(client, worker, "run_routine", routine_id=routine["id"])["error"]
    )
    via_mcp = tool(client, orch, "run_routine", routine_id=routine["id"])
    assert via_mcp["skipped"] == "previous_running"


def test_routines_enabled_is_a_registered_global_setting(client: TestClient) -> None:
    registry = client.get("/api/settings/registry", headers=ADMIN).json()
    [defn] = [d for d in registry if d["key"] == "routines.enabled"]
    assert (defn["default"], defn["scopes"]) == (True, ["global"])
    res = client.put(
        "/api/projects/alpha/settings/routines.enabled",
        json={"value": False},
        headers=ADMIN,
    )
    assert res.status_code == 422
    res = client.put(
        "/api/settings/routines.enabled", json={"value": False}, headers=ADMIN
    )
    assert res.status_code == 200, res.text
