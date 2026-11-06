"""Bounded wait on events (MCP operations 3.3)."""

import asyncio

from api_support import insert_agent
from argus_api.db import Database
from argus_api.eventlog.routes import WAIT_LIMIT, wait
from argus_api.events import record


async def emit(db: Database, subject_id: str = "t-1", n: int = 1) -> None:
    async with db.write() as tx:
        for _ in range(n):
            await record(tx, "test", "task", subject_id, "task.poked")


async def test_returns_at_once_when_events_are_already_there(db: Database) -> None:
    await emit(db, n=3)
    got = await wait(db, 0, wait_s=3600)  # waiting at all would hang the test
    assert [e.kind for e in got.events] == ["task.poked"] * 3
    assert (got.next_cursor, got.timed_out) == (got.events[-1].id, False)
    again = await wait(db, got.next_cursor, wait_s=0)
    assert (again.events, again.next_cursor, again.timed_out) == (
        [],
        got.next_cursor,
        True,
    )


async def test_long_polls_until_a_matching_event(db: Database) -> None:
    cursor = (await wait(db, 0, wait_s=0)).next_cursor
    pending = asyncio.create_task(
        wait(db, cursor, wait_s=3600, subject_type="task", subject_id="t-1")
    )
    await emit(db, "other")  # filtered out: keeps waiting
    await asyncio.sleep(0)
    assert not pending.done()
    await emit(db, "t-1")
    got = await pending
    assert [e.subject_id for e in got.events] == ["t-1"]
    assert not got.timed_out


async def test_times_out_with_the_cursor_unchanged(db: Database) -> None:
    await emit(db)
    current = (await wait(db, 0, wait_s=0)).next_cursor
    got = await wait(db, None, wait_s=0.01)
    assert (got.events, got.next_cursor, got.timed_out) == ([], current, True)


async def test_pages_of_at_most_the_limit(db: Database) -> None:
    await emit(db, n=WAIT_LIMIT + 5)
    first = await wait(db, 0, wait_s=0)
    assert len(first.events) == WAIT_LIMIT
    rest = await wait(db, first.next_cursor, wait_s=0)
    assert len(rest.events) == 5


async def test_command_blocked_is_informational_and_read_scoped(db: Database) -> None:
    await insert_agent(db, "w-1")
    from argus_api.access import leased_projects, worker_event_filter
    from argus_api.auth import Identity, Scope
    from argus_core.events import TASK_COMMAND_BLOCKED, CommandBlocked

    data = CommandBlocked(tool="Bash", rule="rm_root", reason="deletes /").to_data()
    async with db.write() as tx:
        await record(tx, "worker", "task", "t-1", TASK_COMMAND_BLOCKED, data)
        await record(tx, "worker", "task", "t-2", TASK_COMMAND_BLOCKED, data)
        for task in ("t-1", "t-2"):
            await tx.conn.execute(
                "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
                " required_capabilities, priority, state, requires_approval, review,"
                " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project)"
                " VALUES (?, 'generic', 't', '', '[]', '{}', '[]', 0, 'ready',"
                " 0, 'none', 60, 3, 'test', ?, ?, 'global')",
                (task, tx.now, tx.now),
            )
        await tx.conn.execute(
            "INSERT INTO task_lease_grants (task_id, agent_id, attempt, leased_at)"
            " VALUES ('t-1', 'w-1', 1, ?)",
            (tx.now,),
        )
    # Not an inbox row.
    assert (await wait(db, 0, wait_s=0, inbox=True)).events == []
    # A worker reads only the task it has leased; admins read both.
    worker = Identity("tok", Scope.WORKER, "w-1", None)
    async with db.read() as conn:
        scope = worker_event_filter(worker, await leased_projects(conn, "w-1"))
    assert scope is not None
    got = await wait(db, 0, wait_s=0, scope=scope)
    assert [e.subject_id for e in got.events] == ["t-1"]
    assert len((await wait(db, 0, wait_s=0)).events) == 2
