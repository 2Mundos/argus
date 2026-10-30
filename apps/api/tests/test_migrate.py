from pathlib import Path

import aiosqlite
import pytest
from api_support import insert_agent
from argus_api.agents import service as _agents  # noqa: F401 - avoids an import cycle
from argus_api.db import Database, MigrationError, ts
from argus_api.db.migrate import Migration, load_migrations
from argus_api.tasks import service as tasks
from argus_api.tasks import steers
from argus_core.tasks import TaskCreate


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


async def test_0004_rebuild_keeps_existing_tokens(tmp_path: Path) -> None:
    path = tmp_path / "argus.db"
    migrations = load_migrations()
    db = await Database.open(path, migrations=migrations[:3])
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO tokens (id, token_hash, kind, role, created_by, created_at)"
            " VALUES ('t1', 'h1', 'bootstrap', 'worker', 'x', 'now')"
        )
    await db.close()
    db = await Database.open(path, migrations=migrations)
    async with (
        db.read() as conn,
        conn.execute("SELECT id, kind, label FROM tokens") as cur,
    ):
        rows = [tuple(r) for r in await cur.fetchall()]
    await db.close()
    assert rows == [("t1", "bootstrap", None)]


async def test_0057_marks_pending_steers_dispatched(tmp_path: Path) -> None:
    path = tmp_path / "argus.db"
    migrations = load_migrations()
    db = await Database.open(path, migrations=migrations[:56])
    await insert_agent(db, "a")
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        await tasks.claim(tx, "a", [])
        await tx.conn.execute(
            "UPDATE agents SET runtime = '{\"features\": [\"steer\"]}' WHERE id = 'a'"
        )
        waiting = await steers.create(tx, task.id, "waiting", "admin:test")
        await tx.conn.execute(
            "INSERT INTO task_steers (id, task_id, attempt, agent_id, message, actor,"
            " state, created_at, delivered_at) VALUES ('done', ?, 1, 'a', 'x',"
            " 'admin:test', 'delivered', ?, ?)",
            (task.id, ts(tx.now), ts(tx.now)),
        )
    await db.close()

    for _ in range(2):  # the upgrade, then a reopen that must change nothing
        db = await Database.open(path, migrations=migrations)
        async with db.read() as conn:
            async with conn.execute(
                "SELECT id, state, created_at, dispatched_at FROM task_steers"
                " ORDER BY id"
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]
            async with conn.execute("SELECT steer_unknown FROM task_attempts") as cur:
                counts = [r[0] for r in await cur.fetchall()]
        await db.close()
        assert await _user_version(path) == len(migrations)
        by_id = {r[0]: r for r in rows}
        assert by_id["done"][1:] == ("delivered", by_id["done"][2], None)
        assert by_id[waiting.id][1] == "pending"
        assert by_id[waiting.id][3] == by_id[waiting.id][2]  # conservatively carried
        assert counts == [0]

    db = await Database.open(path, migrations=migrations)
    async with db.write() as tx:
        await tasks.release(tx, task.id, "admin:test", "operator")
    async with db.read() as conn:
        page = await steers.list_steers(conn, task.id, 1)
        attempts = await tasks.attempt_definitions(conn, task.id)
    await db.close()
    assert {s.id: s.state for s in page.items} == {
        "done": "delivered",
        waiting.id: "unknown",
    }
    assert [a.steer_unknown for a in attempts] == [1]
