import re
from collections import Counter
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable

import aiosqlite

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# A migration whose SQL holds this line rebuilds a table other tables reference.
# SQLite ignores `PRAGMA foreign_keys` inside a transaction, so the runner turns
# the checks off around it and runs `foreign_key_check` before the commit
# (SQLite's table-rebuild procedure). A broken reference the database already
# had is kept; any other one rolls the migration back.
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
        if rebuild:
            await conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # `executescript` commits whatever is pending before it runs, so the
            # record of existing broken references shares the migration's script,
            # and the script leaves the transaction open for the check.
            before = await _record_broken_references(conn) if rebuild else ""
            await conn.executescript(f"BEGIN IMMEDIATE;\n{before}{m.sql}\n;")
            if rebuild and (added := await _added_broken_references(conn)):
                more = f"; and {len(added) - 5} more" if len(added) > 5 else ""
                raise MigrationError(
                    "broken foreign keys added: " + "; ".join(added[:5]) + more
                )
            await conn.execute(f"PRAGMA user_version = {m.version}")
            await conn.execute("COMMIT")
        except Exception as exc:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise MigrationError(f"{m.name}: {exc}") from exc
        finally:
            if rebuild:
                await conn.execute("PRAGMA foreign_keys = ON")
    return latest


async def _broken_reference_queries(conn: aiosqlite.Connection) -> list[str]:
    """One SELECT per table with foreign keys, giving its broken references as
    (child, row, parent, ref): `row` is the child row's primary key and `ref` its
    referencing columns, both with their values. A reference that changes while
    broken, or moves to another row, is therefore a different one. In a WITHOUT
    ROWID table `foreign_key_check` names no row, so `row` and `ref` are NULL."""
    async with conn.execute(
        'SELECT t.name, t.wr, f.id, f."from" FROM pragma_table_list t'
        " JOIN pragma_foreign_key_list(t.name) f"
        " WHERE t.schema = 'main' AND t.type = 'table'"
        " ORDER BY t.name, f.id, f.seq"
    ) as cur:
        tables: dict[tuple[str, bool], dict[int, list[str]]] = {}
        for table, wr, fk, column in await cur.fetchall():
            tables.setdefault((table, bool(wr)), {}).setdefault(fk, []).append(column)
    queries = []
    for (table, wr), fks in tables.items():
        found = f"FROM pragma_foreign_key_check({_text(table)}) k"
        if wr:
            queries.append(f"SELECT {_text(table)}, NULL, k.parent, NULL {found}")
            continue
        async with conn.execute(
            "SELECT name FROM pragma_table_info(?) WHERE pk > 0 ORDER BY pk", (table,)
        ) as cur:
            key = [r[0] for r in await cur.fetchall()] or ["rowid"]
        ref = " ".join(f"WHEN {fk} THEN {_values(cols)}" for fk, cols in fks.items())
        queries.append(
            f"SELECT {_text(table)}, {_values(key)}, k.parent, CASE k.fkid {ref} END"
            f" {found} JOIN {_name(table)} c ON c.rowid = k.rowid"
        )
    return queries


async def _record_broken_references(conn: aiosqlite.Connection) -> str:
    """SQL that stores the broken references the database has before a rebuild."""
    queries = await _broken_reference_queries(conn)
    inserts = "".join(f"INSERT INTO temp._fk_before {q};\n" for q in queries)
    return f"CREATE TABLE temp._fk_before (child, row, parent, ref);\n{inserts}"


async def _added_broken_references(conn: aiosqlite.Connection) -> list[str]:
    """The broken references the migration added, as recorded by
    `_record_broken_references`. One in a WITHOUT ROWID table cannot be told
    apart from an old one, so it always counts as added."""
    async with conn.execute(
        "SELECT * FROM temp._fk_before WHERE row IS NOT NULL"
    ) as cur:
        before = Counter(tuple(r) for r in await cur.fetchall())
    after: Counter[tuple[str, str | None, str, str | None]] = Counter()
    for query in await _broken_reference_queries(conn):
        async with conn.execute(query) as cur:
            after.update(tuple(r) for r in await cur.fetchall())
    await conn.execute("DROP TABLE temp._fk_before")
    return sorted(
        f"{child} {row} {ref} -> {parent}"
        if row is not None
        else f"{child} -> {parent} (WITHOUT ROWID: row unknown)"
        for child, row, parent, ref in (after - before).elements()
    )


def _values(columns: list[str]) -> str:
    """`a='x', b=1`: `quote()` keeps text, numbers, NULL and blobs apart."""
    return " || ', ' || ".join(
        f"{_text(f'{c}=')} || quote(c.{_name(c)})" for c in columns
    )


def _name(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
