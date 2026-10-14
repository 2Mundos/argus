import asyncio
import json

from argus_api.db import Database
from argus_api.eventlog.routes import event_stream
from argus_api.events import record


async def emit(db: Database, kind: str) -> int:
    async with db.write() as tx:
        return (await record(tx, "test", "thing", "1", kind)).id


def parse(chunk: str) -> dict:
    data = next(line for line in chunk.splitlines() if line.startswith("data: "))
    return json.loads(data.removeprefix("data: "))


async def test_resume_replays_missed_events_then_goes_live(db: Database) -> None:
    first = await emit(db, "a")
    await emit(db, "b")
    stream = event_stream(db, after=first, keepalive=5)
    assert parse(await anext(stream))["kind"] == "b"
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await emit(db, "c")
    chunk = await asyncio.wait_for(pending, 2)
    assert parse(chunk)["kind"] == "c"
    assert chunk.startswith("id: ")
    await stream.aclose()


async def test_fresh_stream_starts_at_now(db: Database) -> None:
    await emit(db, "old")
    stream = event_stream(db, after=None, keepalive=5)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    await emit(db, "new")
    assert parse(await asyncio.wait_for(pending, 2))["kind"] == "new"
    await stream.aclose()


async def test_keepalive_when_idle(db: Database) -> None:
    stream = event_stream(db, after=None, keepalive=0.01)
    assert await asyncio.wait_for(anext(stream), 2) == ": keepalive\n\n"
    await stream.aclose()


async def test_overflowing_subscriber_ends_the_stream(db: Database) -> None:
    stream = event_stream(db, after=None, keepalive=5)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    # Fill the subscriber's queue beyond its bound in one go, without yielding
    # to the stream; it must stop instead of buffering without limit.
    async with db.write() as tx:
        for _ in range(1001):
            await record(tx, "test", "thing", "1", "burst")
    first = await asyncio.wait_for(pending, 2)
    assert parse(first)["kind"] == "burst"
    rest = [chunk async for chunk in stream]  # drains the buffer, then ends
    assert len(rest) + 1 == 1000
