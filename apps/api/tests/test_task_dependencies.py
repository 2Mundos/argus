"""Editing dependencies and stuck dependents (task dependencies spec)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import DONE, insert_agent
from argus_api.db import Database, ts
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN, register, tool

A = "agent-a"


@pytest.fixture(autouse=True)
async def agent(db: Database) -> None:
    await insert_agent(db, A)


async def create(db: Database, deps: list[str] | None = None, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx,
            TaskCreate(project="global", title="t", depends_on=deps or [], **kw),
            "admin:test",
        )


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def set_deps(db: Database, task_id: str, deps: list[str], version=None):
    task = await get(db, task_id)
    async with db.write() as tx:
        return await tasks.set_dependencies(
            tx, task_id, deps, version or ts(task.updated_at), "admin:test"
        )


async def finish(db: Database, task_id: str) -> None:
    """Claim and complete exactly this task (it must be the only ready one)."""
    async with db.write() as tx:
        claimed = await tasks.claim(tx, A, [])
        assert claimed is not None and claimed.id == task_id
        await tasks.complete(tx, A, task_id, claimed.attempt, DONE)


async def test_adding_an_unfinished_dependency_moves_ready_back_to_pending(
    db: Database,
) -> None:
    first = await create(db)
    second = await create(db)
    assert second.state is S.READY
    held = await set_deps(db, second.id, [first.id])
    assert (held.state, held.depends_on) == (S.PENDING, [first.id])
    await finish(db, first.id)  # the existing promotion still applies
    assert (await get(db, second.id)).state is S.READY


async def test_removing_the_last_unfinished_dependency_releases_the_task(
    db: Database,
) -> None:
    first = await create(db)
    second = await create(db, [first.id])
    assert second.state is S.PENDING
    released = await set_deps(db, second.id, [])
    assert (released.state, released.depends_on) == (S.READY, [])


async def test_a_cancelled_dependency_strands_until_removed(db: Database) -> None:
    gone, done = await create(db), await create(db)
    async with db.write() as tx:
        await tasks.cancel(tx, gone.id, "admin:test")
    await finish(db, done.id)
    stuck = await create(db, [gone.id, done.id])
    assert stuck.state is S.PENDING
    released = await set_deps(db, stuck.id, [done.id])
    assert released.state is S.READY


async def test_awaiting_approval_keeps_its_state(db: Database) -> None:
    first = await create(db)
    gated = await create(db, requires_approval=True)
    changed = await set_deps(db, gated.id, [first.id])
    assert (changed.state, changed.depends_on) == (S.AWAITING_APPROVAL, [first.id])
    async with db.write() as tx:
        approved = await tasks.approve(tx, gated.id, "admin:test")
    assert approved.state is S.PENDING  # approval honours the new dependency


async def test_only_waiting_tasks_change(db: Database) -> None:
    task = await create(db)
    async with db.write() as tx:
        await tasks.claim(tx, A, [])
    with pytest.raises(ConflictError) as err:
        await set_deps(db, task.id, [])
    assert err.value.code == "task_not_waiting"


async def test_stale_versions_and_unknown_ids_are_refused(db: Database) -> None:
    task = await create(db)
    with pytest.raises(ConflictError) as err:
        await set_deps(db, task.id, [], version="2000-01-01T00:00:00.000000Z")
    assert err.value.code == "stale_version"
    with pytest.raises(NotFoundError):
        await set_deps(db, task.id, ["nope"])


async def test_cycles_are_refused_with_their_path(db: Database) -> None:
    a = await create(db)
    b = await create(db, [a.id])
    c = await create(db, [b.id])
    with pytest.raises(ValidationFailedError) as err:
        await set_deps(db, a.id, [c.id])
    assert err.value.code == "dependency_cycle"
    assert err.value.details == [a.id, c.id, b.id, a.id]
    with pytest.raises(ValidationFailedError) as err:
        await set_deps(db, a.id, [a.id])
    assert err.value.code == "dependency_cycle"
    assert (await get(db, a.id)).depends_on == []  # nothing changed


# -- HTTP and MCP -------------------------------------------------------------------

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def test_stuck_dependents_show_in_the_inbox_and_are_released_over_mcp(
    client: TestClient,
) -> None:
    def new(**kw) -> dict:
        res = client.post(
            "/api/tasks", json={"project": "global", "title": "t", **kw}, headers=ADMIN
        )
        assert res.status_code == 201, res.text
        return res.json()

    dep = new()
    client.post(f"/api/tasks/{dep['id']}/cancel", headers=ADMIN).raise_for_status()
    stuck = new(depends_on=[dep["id"]])
    new(depends_on=[new()["id"]])  # pending on a ready task: not stuck

    page = client.get("/api/inbox", headers=ADMIN).json()
    [row] = page["items"]
    assert (row["subject_id"], row["state"], row["failed_dependencies"]) == (
        stuck["id"],
        "pending",
        [dep["id"]],
    )
    assert row["decision_id"] is None
    assert page["totals_by_state"] == {"pending": 1}
    assert page["totals_by_decision_kind"] == {"none": 1}
    only = client.get("/api/inbox", params={"state": "blocked"}, headers=ADMIN)
    assert only.json()["total"] == 0

    worker = {"Authorization": f"Bearer {register(client)}"}
    body = {"depends_on": [], "expected_version": row["version"]}
    refused = client.put(
        f"/api/tasks/{stuck['id']}/dependencies", json=body, headers=worker
    )
    assert refused.status_code == 403

    orchestrator = register(client, "orchestrator")
    released = tool(
        client,
        orchestrator,
        "set_task_dependencies",
        task_id=stuck["id"],
        depends_on=[],
        expected_version=row["version"],
    )
    assert (released["state"], released["depends_on"]) == ("ready", [])
    assert client.get("/api/inbox", headers=ADMIN).json()["total"] == 0
    events = client.get(
        "/api/events", params={"subject_id": stuck["id"]}, headers=ADMIN
    ).json()
    changed = next(e for e in events if e["kind"] == "task.dependencies_changed")
    assert changed["data"] == {"added": [], "removed": [dep["id"]]}
