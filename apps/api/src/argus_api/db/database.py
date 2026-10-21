import asyncio
import contextlib
import itertools
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from argus_api.db.migrate import Migration, load_migrations, migrate
from argus_api.events import Event, EventBus

_BUSY_TIMEOUT_MS = 5000


def utcnow() -> datetime:
    return datetime.now(UTC)


def ts(dt: datetime) -> str:
    """Fixed-width ISO-8601 UTC text: sorts and compares correctly as a string."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


Clock = Callable[[], datetime]


_savepoints = itertools.count(1)


@dataclass(frozen=True)
class Tx:
    """An open write transaction.

    `now` is read after `BEGIN IMMEDIATE` succeeded, so time spent waiting for the
    writer can never make an expired lease look valid (worker lifecycle 3.3).
    """

    conn: aiosqlite.Connection
    now: datetime
    events: list[Event] = field(default_factory=list)
    transient: list[Event] = field(default_factory=list)  # published, never stored

    @asynccontextmanager
    async def savepoint(self) -> AsyncGenerator[None]:
        """Run a unit of work that may fail without aborting the transaction.

        On an exception its writes are rolled back and its events dropped (they
        must never be published), then the exception propagates for the caller
        to handle; the rest of the transaction is untouched.
        """
        name = f"sp_{next(_savepoints)}"
        mark = len(self.events)
        transient_mark = len(self.transient)
        await self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            await self.conn.execute(f"ROLLBACK TO {name}")
            await self.conn.execute(f"RELEASE {name}")
            del self.events[mark:]
            del self.transient[transient_mark:]
            raise
        await self.conn.execute(f"RELEASE {name}")


@dataclass(frozen=True)
class Snapshot:
    """An open read transaction: every query on `conn` sees one consistent state.

    `now` is read once, after `BEGIN`, so ages computed from it agree with the data.
    """

    conn: aiosqlite.Connection
    now: datetime


class Database:
    """One serialized writer connection plus a pool of read-only connections."""

    def __init__(
        self,
        writer: aiosqlite.Connection,
        readers: list[aiosqlite.Connection],
        clock: Clock = utcnow,
    ) -> None:
        self.bus = EventBus()
        self._clock = clock
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._all_readers = readers
        for r in readers:
            self._readers.put_nowait(r)

    @classmethod
    async def open(
        cls,
        path: Path,
        *,
        readers: int = 4,
        migrations: list[Migration] | None = None,
        clock: Clock = utcnow,
    ) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = await _connect(str(path))
        try:
            await writer.executescript(
                "PRAGMA journal_mode = WAL;"
                "PRAGMA synchronous = NORMAL;"
                "PRAGMA foreign_keys = ON;"
            )
            await migrate(
                writer, load_migrations() if migrations is None else migrations
            )
            pool = [
                await _connect(f"file:{path}?mode=ro", uri=True) for _ in range(readers)
            ]
        except BaseException:
            await writer.close()
            raise
        return cls(writer, pool, clock)

    async def close(self) -> None:
        for r in self._all_readers:
            await r.close()
        await self._writer.close()

    @asynccontextmanager
    async def write(self) -> AsyncGenerator[Tx]:
        """Run one write transaction; commit on success, roll back on any error.

        Events recorded in the transaction are published only after commit.
        """
        async with self._write_lock:
            await self._writer.execute("BEGIN IMMEDIATE")
            tx = Tx(self._writer, self._clock())
            try:
                yield tx
            except BaseException:
                await self._writer.execute("ROLLBACK")
                raise
            try:
                await self._writer.execute("COMMIT")
            except BaseException:
                # A cancelled COMMIT may still complete in aiosqlite's thread: roll
                # back only what is still open, and never let a failed ROLLBACK
                # replace the original error (a swallowed CancelledError leaves a
                # background loop running forever).
                if self._writer.in_transaction:
                    with contextlib.suppress(Exception):
                        await self._writer.execute("ROLLBACK")
                raise
        for event in (*tx.events, *tx.transient):
            self.bus.publish(event)

    @asynccontextmanager
    async def read(self) -> AsyncGenerator[aiosqlite.Connection]:
        conn = await self._readers.get()
        try:
            yield conn
        finally:
            self._readers.put_nowait(conn)

    @asynccontextmanager
    async def snapshot(self) -> AsyncGenerator[Snapshot]:
        """Read in one transaction, so totals and pages come from the same state."""
        async with self.read() as conn:
            await conn.execute("BEGIN")
            try:
                yield Snapshot(conn, self._clock())
            finally:
                await conn.execute("COMMIT")


async def _connect(database: str, *, uri: bool = False) -> aiosqlite.Connection:
    # isolation_level=None: no implicit transactions; we issue BEGIN/COMMIT ourselves.
    conn = await aiosqlite.connect(database, uri=uri, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn
