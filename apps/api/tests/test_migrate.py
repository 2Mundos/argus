from pathlib import Path

import aiosqlite
import pytest
from argus_api.db import Database, MigrationError
from argus_api.db.migrate import Migration, load_migrations


async def _user_version(path: Path) -> int:
    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("PRAGMA user_version") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return row[0]


def test_shipped_migrations_load_in_order() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))


def test_gap_in_versions_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="expected migration 0002"):
        load_migrations(tmp_path)


async def test_failed_migration_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "argus.db"
    good = Migration(1, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    bad = Migration(2, "0002_b.sql", "CREATE TABLE b (x INTEGER); NOT VALID SQL;")
    with pytest.raises(MigrationError, match=r"0002_b\.sql"):
        await Database.open(path, migrations=[good, bad])
    assert await _user_version(path) == 1
    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT name FROM sqlite_master WHERE name = 'b'") as cur,
    ):
        assert await cur.fetchone() is None


async def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "argus.db"
    one = Migration(1, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    two = Migration(2, "0002_b.sql", "CREATE TABLE b (x INTEGER);")
    db = await Database.open(path, migrations=[one, two])
    await db.close()
    with pytest.raises(MigrationError, match="knows up to 1"):
        await Database.open(path, migrations=[one])
