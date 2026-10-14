import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable

import aiosqlite

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(
    directory: Traversable = files("argus_api.db") / "migrations",
) -> list[Migration]:
    """Read `NNNN_name.sql` files; versions must run 1, 2, 3, … without gaps."""
    found = sorted(
        (int(m[1]), entry.name, entry.read_text())
        for entry in directory.iterdir()
        if (m := _NAME.match(entry.name))
    )
    for expected, (version, name, _) in enumerate(found, start=1):
        if version != expected:
            raise MigrationError(f"expected migration {expected:04d}, found {name}")
    return [Migration(v, n, sql) for v, n, sql in found]


async def migrate(conn: aiosqlite.Connection, migrations: list[Migration]) -> int:
    """Apply pending migrations, each atomically with its `user_version` bump."""
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current = row[0] if row else 0
    latest = len(migrations)
    if current > latest:
        raise MigrationError(
            f"database is at version {current}, this build knows up to {latest}"
        )
    for m in migrations[current:]:
        try:
            await conn.executescript(
                f"BEGIN IMMEDIATE;\n{m.sql}\n;\n"
                f"PRAGMA user_version = {m.version};\nCOMMIT;"
            )
        except Exception as exc:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise MigrationError(f"{m.name}: {exc}") from exc
    return latest
