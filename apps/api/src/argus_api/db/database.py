import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from argus_api.db.migrate import Migration, load_migrations, migrate

_BUSY_TIMEOUT_MS = 5000


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Tx:
    """An open write transaction.

    `now` is read after `BEGIN IMMEDIATE` succeeded, so time spent waiting for the
    writer can never make an expired lease look valid (worker lifecycle 3.3).
    """

    conn: aiosqlite.Connection
    now: datetime


class Database:
    """One serialized writer connection plus a pool of read-only connections."""

    def __init__(
        self, writer: aiosqlite.Connection, readers: list[aiosqlite.Connection]
    ) -> None:
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
        return cls(writer, pool)

    async def close(self) -> None:
        for r in self._all_readers:
            await r.close()
        await self._writer.close()

    @asynccontextmanager
    async def write(self) -> AsyncGenerator[Tx]:
        """Run one write transaction; commit on success, roll back on any error."""
        async with self._write_lock:
            await self._writer.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(self._writer, utcnow())
            except BaseException:
                await self._writer.execute("ROLLBACK")
                raise
            await self._writer.execute("COMMIT")

    @asynccontextmanager
    async def read(self) -> AsyncGenerator[aiosqlite.Connection]:
        conn = await self._readers.get()
        try:
            yield conn
        finally:
            self._readers.put_nowait(conn)


async def _connect(database: str, *, uri: bool = False) -> aiosqlite.Connection:
    # isolation_level=None: no implicit transactions; we issue BEGIN/COMMIT ourselves.
    conn = await aiosqlite.connect(database, uri=uri, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn
