import asyncio
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api.db import Database


async def _count_events(db: Database) -> int:
    async with db.read() as conn, conn.execute("SELECT count(*) FROM events") as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_event(conn, kind: str = "test") -> None:
    await conn.execute(
        "INSERT INTO events (at, actor, subject_type, subject_id, kind)"
        " VALUES ('t', 'a', 's', '1', ?)",
        (kind,),
    )


async def test_write_commits(db: Database) -> None:
    async with db.write() as tx:
        await _insert_event(tx.conn)
    assert await _count_events(db) == 1


async def test_write_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.write() as tx:
            await _insert_event(tx.conn)
            raise RuntimeError("boom")
    assert await _count_events(db) == 0
    # The writer is usable again after a rollback.
    async with db.write() as tx:
        await _insert_event(tx.conn)
    assert await _count_events(db) == 1


async def test_a_cancel_after_commit_stays_a_cancel(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit ran but the caller was cancelled meanwhile: the CancelledError
    must reach the caller (a failed ROLLBACK must not replace it), or a cancelled
    background loop would swallow it and never stop."""
    writer = db._writer
    real = writer.execute

    async def commit_then_cancel(sql: str, *args):
        result = await real(sql, *args)
        if sql == "COMMIT":
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(writer, "execute", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        async with db.write() as tx:
            await _insert_event(tx.conn)
    monkeypatch.undo()
    assert await _count_events(db) == 1  # committed
    async with db.write() as tx:  # and the writer is still usable
        await _insert_event(tx.conn)
    assert await _count_events(db) == 2


async def test_writes_are_serialized(db: Database) -> None:
    order: list[str] = []

    async def writer(name: str) -> None:
        async with db.write():
            order.append(f"{name}:start")
            await asyncio.sleep(0.01)
            order.append(f"{name}:end")

    await asyncio.gather(writer("a"), writer("b"), writer("c"))
    pairs = [order[i : i + 2] for i in range(0, len(order), 2)]
    assert all(p[0].split(":")[0] == p[1].split(":")[0] for p in pairs)


async def test_tx_now_is_read_after_waiting_for_the_writer(
    db: Database, clock: FakeClock
) -> None:
    async with db.write() as first:
        waiter = asyncio.create_task(_now_of_next_write(db))
        await asyncio.sleep(0)  # the waiter is now queued on the writer
        clock.advance(5)
    assert (await waiter - first.now).total_seconds() == 5


async def _now_of_next_write(db: Database):
    async with db.write() as tx:
        return tx.now


async def test_readers_cannot_write(db: Database) -> None:
    async with db.read() as conn:
        with pytest.raises(Exception, match="readonly"):
            await _insert_event(conn)


async def test_reopen_keeps_data_and_applies_nothing_new(tmp_path: Path) -> None:
    path = tmp_path / "argus.db"
    db = await Database.open(path)
    async with db.write() as tx:
        await _insert_event(tx.conn)
    await db.close()
    db = await Database.open(path)
    assert await _count_events(db) == 1
    await db.close()


async def test_wal_mode(db: Database) -> None:
    async with db.read() as conn, conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "wal"
