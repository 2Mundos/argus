"""Bounded wait on events (MCP operations 3.3)."""

import asyncio
import time

from argus_api.db import Database
from argus_api.eventlog.routes import WAIT_LIMIT, wait
from argus_api.events import record


async def emit(db: Database, subject_id: str = "t-1", n: int = 1) -> None:
    async with db.write() as tx:
        for _ in range(n):
            await record(tx, "test", "task", subject_id, "task.poked")


async def test_returns_at_once_when_events_are_already_there(db: Database) -> None:
    await emit(db, n=3)
    started = time.monotonic()
    got = await wait(db, 0, wait_s=10)
    assert time.monotonic() - started < 1
    assert [e.kind for e in got.events] == ["task.poked"] * 3
    assert (got.next_cursor, got.timed_out) == (got.events[-1].id, False)
    again = await wait(db, got.next_cursor, wait_s=0)
    assert (again.events, again.next_cursor, again.timed_out) == (
        [],
        got.next_cursor,
        True,
    )


async def test_long_polls_until_a_matching_event(db: Database) -> None:
    async def later() -> None:
        await asyncio.sleep(0.2)
        await emit(db, "other")  # filtered out: keeps waiting
        await asyncio.sleep(0.2)
        await emit(db, "t-1")

    poke = asyncio.create_task(later())
    started = time.monotonic()
    got = await wait(db, None, wait_s=5, subject_type="task", subject_id="t-1")
    await poke
    assert 0.3 < time.monotonic() - started < 3
    assert [e.subject_id for e in got.events] == ["t-1"]
    assert not got.timed_out


async def test_times_out_with_the_cursor_unchanged(db: Database) -> None:
    await emit(db)
    current = (await wait(db, 0, wait_s=0)).next_cursor
    started = time.monotonic()
    got = await wait(db, None, wait_s=0.3)
    assert time.monotonic() - started >= 0.3
    assert (got.events, got.next_cursor, got.timed_out) == ([], current, True)


async def test_pages_of_at_most_the_limit(db: Database) -> None:
    await emit(db, n=WAIT_LIMIT + 5)
    first = await wait(db, 0, wait_s=0)
    assert len(first.events) == WAIT_LIMIT
    rest = await wait(db, first.next_cursor, wait_s=0)
    assert len(rest.events) == 5
