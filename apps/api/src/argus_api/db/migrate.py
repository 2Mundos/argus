import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable

import aiosqlite

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# A migration whose SQL holds this line rebuilds a table other tables reference.
# SQLite ignores `PRAGMA foreign_keys` inside a transaction, so the runner turns
# the checks off around it and runs `foreign_key_check` before the commit
# (SQLite's table-rebuild procedure).
REBUILDS_REFERENCED = "-- argus: foreign keys off"


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
        rebuild = REBUILDS_REFERENCED in m.sql
        # One script per migration: `executescript` commits whatever is pending
        # before it runs, so the check must sit inside it. A broken reference
        # fails the CHECK and the whole migration rolls back.
        check = (
            "CREATE TEMP TABLE _fk_broken (n INTEGER CHECK (n = 0));\n"
            "INSERT INTO _fk_broken SELECT count(*) FROM pragma_foreign_key_check;\n"
            "DROP TABLE _fk_broken;\n"
            if rebuild
            else ""
        )
        if rebuild:
            await conn.execute("PRAGMA foreign_keys = OFF")
        try:
            await conn.executescript(
                f"BEGIN IMMEDIATE;\n{m.sql}\n;\n{check}"
                f"PRAGMA user_version = {m.version};\nCOMMIT;"
            )
        except Exception as exc:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise MigrationError(f"{m.name}: {exc}") from exc
        finally:
            if rebuild:
                await conn.execute("PRAGMA foreign_keys = ON")
    return latest
