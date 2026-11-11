# ruff: noqa: F811  (the `client` fixture is imported from test_mcp)
"""The project task graph (task graph spec)."""

import json
from datetime import timedelta

import pytest
from api_support import FakeClock, insert_agent, insert_project
from argus_api.access import worker_task_filter
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.tasks import graph
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S
from test_mcp import ADMIN_TOKEN, client, register, tool  # noqa: F401  (fixture)

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(autouse=True)
def _clock(clock: FakeClock) -> None:
    global CLOCK
    CLOCK = clock


CLOCK: FakeClock


async def create(
    db: Database, title: str, deps: list[str] | None = None, project: str = "demo"
) -> str:
    CLOCK.advance(1)  # creation order is then the order by created_at
    async with db.write() as tx:
        task = await tasks.create_task(
            tx,
            TaskCreate(project=project, title=title, depends_on=deps or []),
            "admin:test",
        )
    return task.id


async def sql(db: Database, statement: str, *params: object) -> None:
    async with db.write() as tx:
        await tx.conn.execute(statement, params)


async def lease(db: Database, agent: str, *task_ids: str) -> Identity:
    await insert_agent(db, agent)
    for task_id in task_ids:
        await sql(
            db,
            "INSERT INTO task_lease_grants (task_id, agent_id, attempt, leased_at)"
            " VALUES (?, ?, 1, '2026-01-01T00:00:00Z')",
            task_id,
            agent,
        )
    return Identity("tok", Scope.WORKER, agent, None)


async def read(
    db: Database, caller: Identity | None = None, project: str = "demo", **kw
) -> graph.TaskGraph:
    scope = worker_task_filter(caller) if caller else None
    async with db.snapshot() as snap:
        return await graph.task_graph(snap, project, task_scope=scope, **kw)


def titles(g: graph.TaskGraph) -> list[str]:
    return [n.title for n in g.nodes]


def edges(g: graph.TaskGraph) -> set[tuple[str, str]]:
    by_id = {n.id: n.title for n in g.nodes}
    return {(by_id[e.from_], by_id[e.to]) for e in g.edges}


def assert_closed(g: graph.TaskGraph) -> None:
    ids = {n.id for n in g.nodes}
    assert len(ids) == len(g.nodes)
    assert all(e.from_ in ids and e.to in ids for e in g.edges)


async def test_nodes_and_edges_are_ordered_and_closed(db: Database) -> None:
    a = await create(db, "a")
    b = await create(db, "b", [a])
    c = await create(db, "c", [a, b])
    await create(db, "d")
    await sql(
        db, "UPDATE tasks SET created_at = ? WHERE id = ?", "2025-12-31T00:00:00Z", c
    )
    g = await read(db)
    assert_closed(g)
    assert titles(g) == ["c", "a", "b", "d"]  # by created_at, then id
    assert [(e.from_, e.to) for e in g.edges] == sorted(
        (e.from_, e.to) for e in g.edges
    )
    assert edges(g) == {("a", "b"), ("a", "c"), ("b", "c")}
    assert not g.truncated
    assert g == await read(db)  # same snapshot data, same bytes


async def test_the_cap_keeps_edges_closed_and_truncated_is_truthful(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "MAX_NODES", 3)
    old = await create(db, "old")
    for name in ("t2", "t3", "t4"):
        await create(db, name)
    await create(db, "newest", [old])
    g = await read(db)
    # newest brings old (2 nodes), t4 makes 3, t3 would be the fourth.
    assert titles(g) == ["old", "t4", "newest"] or set(titles(g)) == {
        "old",
        "t4",
        "newest",
    }
    assert_closed(g)
    assert g.truncated
    # Everything fits: not truncated, even with exactly cap nodes.
    monkeypatch.setattr(graph, "MAX_NODES", 5)
    full = await read(db)
    assert len(full.nodes) == 5 and not full.truncated
    # More independent candidates than the cap.
    monkeypatch.setattr(graph, "MAX_NODES", 4)
    over = await read(db)
    assert len(over.nodes) == 4 and over.truncated
    assert_closed(over)


async def test_a_chain_longer_than_the_cap_drops_the_dependent(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "MAX_NODES", 2)
    a = await create(db, "a")
    b = await create(db, "b", [a])
    await create(db, "c", [b])
    g = await read(db)  # c needs a, b, c: more than the cap, so nothing fits
    assert g.nodes == [] and g.truncated


async def test_done_cutoff_include_done_and_archive(
    db: Database, clock: FakeClock
) -> None:
    old_done = await create(db, "old-done")
    needed = await create(db, "needed-done")
    await create(db, "waits", [needed])
    archived = await create(db, "archived")
    await create(db, "dependent-of-old", [old_done])
    for task_id in (old_done, needed):
        await sql(db, "UPDATE tasks SET state = 'succeeded' WHERE id = ?", task_id)
    await sql(db, "UPDATE tasks SET archived_at = updated_at WHERE id = ?", archived)
    clock.advance(timedelta(days=8).total_seconds())
    g = await read(db)
    # old-done's only dependent is unfinished too, so it stays; archived never shows.
    assert set(titles(g)) == {"old-done", "needed-done", "waits", "dependent-of-old"}
    await sql(
        db,
        "UPDATE tasks SET state = 'succeeded' WHERE title = 'dependent-of-old'",
    )
    g = await read(db)
    assert set(titles(g)) == {"needed-done", "waits"}  # old-done fell out
    assert_closed(g)
    everything = await read(db, include_done=True)
    assert set(titles(everything)) == {
        "old-done",
        "needed-done",
        "waits",
        "dependent-of-old",
    }
    # A recent succeeded task stays.
    await sql(
        db,
        "UPDATE tasks SET updated_at = ? WHERE title = 'old-done'",
        "2026-01-09T00:00:00Z",
    )
    assert "old-done" in titles(await read(db))


async def test_failed_dependencies_mark_a_stuck_pending_node(db: Database) -> None:
    dep = await create(db, "dep")
    good = await create(db, "good")
    stuck = await create(db, "stuck", [dep, good])
    await sql(db, "UPDATE tasks SET state = 'failed' WHERE id = ?", dep)
    g = await read(db)
    by = {n.title: n for n in g.nodes}
    assert by["stuck"].failed_dependencies == [dep]
    assert by["stuck"].state is S.PENDING and by["good"].failed_dependencies == []
    assert stuck in {n.id for n in g.nodes}


async def test_cross_project_dependency_is_a_stub_only_when_visible(
    db: Database,
) -> None:
    await insert_project(db, "other")
    secret = await create(db, "secret title", project="other")
    seen = await create(db, "visible stub", project="other")
    mine = await create(db, "mine", [secret, seen])
    own = await create(db, "own")
    manager = await read(db)
    assert {n.title: n.project for n in manager.nodes} == {
        "mine": "demo",
        "own": "demo",
        "secret title": "other",
        "visible stub": "other",
    }
    assert manager.edges and all(e.to == mine for e in manager.edges)
    stub = next(n for n in manager.nodes if n.title == "secret title")
    assert stub.failed_dependencies == [] and stub.block_reason is None

    worker = await lease(db, "w-1", mine, own, seen)
    scoped = await read(db, worker)
    dump = json.dumps(scoped.model_dump(by_alias=True), default=str)
    assert "secret title" not in dump and secret not in dump
    assert titles(scoped) == ["visible stub", "mine", "own"]
    assert edges(scoped) == {("visible stub", "mine")}
    # A failed unseen dependency does not show up as `failed_dependencies` either.
    await sql(db, "UPDATE tasks SET state = 'failed' WHERE id = ?", secret)
    again = await read(db, worker)
    assert next(n for n in again.nodes if n.id == mine).failed_dependencies == []
    assert secret not in json.dumps(again.model_dump(by_alias=True), default=str)


async def test_a_worker_only_sees_its_leased_tasks_and_their_edges(
    db: Database,
) -> None:
    a = await create(db, "a")
    b = await create(db, "b", [a])
    c = await create(db, "c", [b])
    worker = await lease(db, "w-1", b, c)
    g = await read(db, worker)
    assert set(titles(g)) == {"b", "c"} and edges(g) == {("b", "c")}
    assert_closed(g)
    assert set(titles(await read(db))) == {"a", "b", "c"}
    # An old succeeded task whose only unfinished dependent is out of scope is
    # judged by the dependents the caller can see.
    other = await lease(db, "w-2", a)
    assert titles(await read(db, other)) == ["a"]


async def test_old_done_cutoff_ignores_unfinished_dependents_out_of_scope(
    db: Database, clock: FakeClock
) -> None:
    a = await create(db, "a")
    await create(db, "hidden dependent", [a])
    await sql(db, "UPDATE tasks SET state = 'succeeded' WHERE id = ?", a)
    worker = await lease(db, "w-1", a)
    clock.advance(timedelta(days=8).total_seconds())
    assert titles(await read(db, worker)) == []
    assert titles(await read(db)) == ["a", "hidden dependent"]


def test_route_and_mcp_share_the_service(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def post(title: str, deps: list[str] | None = None) -> str:
        res = client.post(
            "/api/tasks",
            json={"project": "demo", "title": title, "depends_on": deps or []},
            headers=ADMIN,
        )
        res.raise_for_status()
        return res.json()["id"]

    first = post("first")
    second = post("second", [first])
    calls: list[str] = []
    real = graph.task_graph

    async def spy(snap, project, **kw):
        calls.append(project)
        return await real(snap, project, **kw)

    monkeypatch.setattr(graph, "task_graph", spy)
    res = client.get("/api/projects/demo/task-graph", headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["edges"] == [{"from": first, "to": second}]
    assert [n["title"] for n in body["nodes"]] == ["first", "second"]
    assert body["truncated"] is False
    orchestrator = register(client, "orchestrator")
    assert tool(client, orchestrator, "get_task_graph", project="demo") == body
    assert calls == ["demo", "demo"]

    assert client.get("/api/projects/nope/task-graph", headers=ADMIN).status_code == 404
    worker = {"Authorization": f"Bearer {register(client)}"}
    forbidden = client.get("/api/projects/demo/task-graph", headers=worker)
    assert forbidden.status_code == 403  # no leased task in the project
