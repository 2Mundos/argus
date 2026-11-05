import re
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from api_support import insert_agent, insert_project
from argus_api.accounts import service as accounts_service
from argus_api.agents import service as _agents  # noqa: F401 - avoids an import cycle
from argus_api.db import Database, MigrationError, ts
from argus_api.db.migrate import Migration, load_migrations
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_api.tasks import steers
from argus_core.accounts import AccountCreate
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
    await insert_project(db)
    async with db.write() as tx:
        # claim writes a column that a later migration adds; borrow it for the setup
        await tx.conn.execute("ALTER TABLE tasks ADD COLUMN session_gone_at TEXT")
        await tx.conn.execute("ALTER TABLE tasks ADD COLUMN paused_disk_at TEXT")
        await tx.conn.execute(
            "ALTER TABLE tasks ADD COLUMN deliverable TEXT NOT NULL DEFAULT 'response'"
        )
        task = await tasks.create_task(
            tx, TaskCreate(project="demo", title="t"), "admin:test"
        )
        await tasks.claim(tx, "a", [])
        await tx.conn.execute("ALTER TABLE tasks DROP COLUMN session_gone_at")
        await tx.conn.execute("ALTER TABLE tasks DROP COLUMN paused_disk_at")
        await tx.conn.execute("ALTER TABLE tasks DROP COLUMN deliverable")
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


AUTH_HEALTH_INDEXES = ("account_usage_last_accepted", "events_account_auth_rejected")


async def _accounts(db: Database, *names: str) -> list[str]:
    store = MemorySecretStore()
    ids = []
    for name in names:
        await store.put(name, {"api_key": f"key-{name}"})
        async with db.write() as tx:
            acct = await accounts_service.create_account(
                tx,
                name,
                AccountCreate(
                    name=name, provider="anthropic", secret_ref=f"sops:{name}"
                ),
                f"sops:{name}",
                "admin:test",
                store,
            )
        ids.append(acct.id)
    return ids


async def _fill(db: Database, account_ids: list[str], rows: int) -> None:
    """`rows` usage rows and auth rejections per account, all qualifying evidence."""
    async with db.write() as tx:
        for account_id in account_ids:
            await tx.conn.executemany(
                "INSERT INTO account_usage (account_id, output_tokens, at)"
                " VALUES (?, 1, ?)",
                [
                    (account_id, f"2026-01-01T00:00:{i % 60:02d}.{i:06d}Z")
                    for i in range(rows)
                ],
            )
            await tx.conn.executemany(
                "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
                " VALUES (?, 'agent:a', 'account', ?, 'account.auth_rejected', '{}')",
                [(f"2026-01-01T00:00:00.{i:06d}Z", account_id) for i in range(rows)],
            )


async def test_0060_auth_health_reads_use_the_partial_indexes(tmp_path: Path) -> None:
    """The production read model (`_SELECT`) seeks both 0060 indexes per account."""
    db = await Database.open(tmp_path / "argus.db")
    [account_id] = await _accounts(db, "claude-1")
    async with (
        db.read() as conn,
        conn.execute(
            "EXPLAIN QUERY PLAN " + accounts_service._SELECT + " WHERE a.id = :id",
            {"id": account_id, "since": "2026-01-01T00:00:00Z"},
        ) as cur,
    ):
        plan = [r[3] for r in await cur.fetchall()]
    await db.close()
    assert any(
        d.startswith("SEARCH u USING INDEX account_usage_last_accepted (account_id=?")
        for d in plan
    ), plan
    assert any(
        d.startswith("SEARCH e USING COVERING INDEX events_account_auth_rejected")
        and "subject_id=?" in d
        and "kind=?" in d
        for d in plan
    ), plan


async def test_0060_auth_health_lookup_is_bounded(tmp_path: Path) -> None:
    """Per-account lookup cost does not grow with other accounts' history: the
    SQLite VM steps for the target stay flat while the tables grow 100x."""
    db = await Database.open(tmp_path / "argus.db")
    target, *others = await _accounts(db, "target", "busy-1", "busy-2")
    await _fill(db, [target], 5)
    sql = (
        f"SELECT {accounts_service.LAST_ACCEPTED_SQL},"
        f" {accounts_service.LAST_REJECTED_SQL} FROM accounts a WHERE a.id = ?"
    )

    async def steps() -> int:
        count = 0

        def tick() -> int:
            nonlocal count
            count += 1
            return 0

        async with db.read() as conn:
            await (await conn.execute(sql, (target,))).fetchall()  # warm the cache
            await conn.set_progress_handler(tick, 1)
            try:
                async with conn.execute(sql, (target,)) as cur:
                    row = await cur.fetchone()
            finally:
                await conn.set_progress_handler(None, 1)
        assert row is not None and row[0] is not None and row[1] is not None
        return count

    await _fill(db, others, 50)
    small = await steps()
    await _fill(db, others, 5000)
    large = await steps()
    await db.close()
    assert large <= small + 10, (small, large)


async def test_0060_upgrade_keeps_data_and_reopen_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "argus.db"
    migrations = load_migrations()
    assert migrations[59].name == "0060_account_auth_health_indexes.sql"
    db = await Database.open(path, migrations=migrations[:59])
    [account_id] = await _accounts(db, "claude-1")
    await _fill(db, [account_id], 3)
    await db.close()
    assert await _user_version(path) == 59

    for _ in range(2):  # the upgrade, then a reopen that must change nothing
        db = await Database.open(path, migrations=migrations)
        async with db.read() as conn:
            async with conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
                " AND name IN (?, ?) ORDER BY name",
                AUTH_HEALTH_INDEXES,
            ) as cur:
                indexes = [tuple(r) for r in await cur.fetchall()]
            acct = await accounts_service.get_account(
                conn, account_id, datetime(2026, 1, 2, tzinfo=UTC)
            )
        await db.close()
        assert await _user_version(path) == len(migrations)
        assert [name for name, _ in indexes] == sorted(AUTH_HEALTH_INDEXES)
        assert all("WHERE" in sql for _, sql in indexes)  # partial indexes
        assert acct.last_accepted_at == datetime(2026, 1, 1, 0, 0, 2, 2, tzinfo=UTC)
        assert acct.last_rejected_at == datetime(2026, 1, 1, 0, 0, 0, 2, tzinfo=UTC)


_PARENT_CHILD = Migration(
    1,
    "0001_a.sql",
    "CREATE TABLE p (name TEXT PRIMARY KEY);"
    " CREATE TABLE c (id TEXT PRIMARY KEY, pool TEXT NOT NULL REFERENCES p (name));"
    " INSERT INTO p VALUES ('x'); INSERT INTO c VALUES ('c1', 'x');",
)
_REBUILD_P = (
    "CREATE TABLE p_new (name TEXT PRIMARY KEY); INSERT INTO p_new SELECT * FROM p;"
    " DROP TABLE p; ALTER TABLE p_new RENAME TO p;"
)
# What an earlier write with foreign keys off left behind.
_ORPHAN = "INSERT INTO c VALUES ('c2', 'gone')"


async def _leave_behind(path: Path, statements: list[str]) -> None:
    await (await Database.open(path, migrations=[_PARENT_CHILD])).close()
    async with aiosqlite.connect(path) as conn:  # foreign keys are off by default
        for statement in statements:
            await conn.execute(statement)
        await conn.commit()


def _rebuild(sql: str) -> list[Migration]:
    sql = f"-- argus: foreign keys off\n{sql}"
    return [_PARENT_CHILD, Migration(2, "0002_b.sql", sql)]


async def _contents(path: Path) -> list[list[tuple]]:
    async with aiosqlite.connect(path) as conn:
        found = []
        for sql in (
            "SELECT * FROM p ORDER BY name",
            "SELECT * FROM c ORDER BY id",
            'SELECT "table", parent FROM pragma_foreign_key_check ORDER BY 1, 2',
        ):
            async with conn.execute(sql) as cur:
                found.append([tuple(r) for r in await cur.fetchall()])
    return found


@pytest.mark.parametrize(
    ("left_behind", "sql"),
    [
        pytest.param([], _REBUILD_P, id="clean"),
        pytest.param([_ORPHAN], _REBUILD_P, id="existing broken reference"),
        pytest.param(
            [_ORPHAN],
            "CREATE TABLE c_new (id TEXT PRIMARY KEY,"
            " pool TEXT NOT NULL REFERENCES p (name));"
            " INSERT INTO c_new SELECT * FROM c ORDER BY id DESC;"
            " DROP TABLE c; ALTER TABLE c_new RENAME TO c;" + _REBUILD_P,
            id="child rebuild that renumbers rowids",
        ),
    ],
)
async def test_a_rebuild_keeps_the_broken_references_it_found(
    tmp_path: Path, left_behind: list[str], sql: str
) -> None:
    """A dangling reference the database already had does not block a rebuild."""
    path = tmp_path / "argus.db"
    await _leave_behind(path, left_behind)
    before = await _contents(path)
    await (await Database.open(path, migrations=_rebuild(sql))).close()
    assert await _user_version(path) == 2
    assert await _contents(path) == before


@pytest.mark.parametrize(
    ("left_behind", "sql", "added"),
    [
        pytest.param(
            [],
            "CREATE TABLE p_new (name TEXT PRIMARY KEY);"
            " DROP TABLE p; ALTER TABLE p_new RENAME TO p;",
            "c id='c1' pool='x' -> p",
            id="parent row lost",
        ),
        pytest.param(
            [_ORPHAN],
            "CREATE TABLE p_new (name TEXT PRIMARY KEY);"
            " DROP TABLE p; ALTER TABLE p_new RENAME TO p;",
            "c id='c1' pool='x' -> p",
            id="parent row lost beside an existing broken reference",
        ),
        pytest.param(
            [_ORPHAN],
            "UPDATE c SET pool = 'lost' WHERE id = 'c2';" + _REBUILD_P,
            "c id='c2' pool='lost' -> p",
            id="broken reference changed",
        ),
        pytest.param(
            [_ORPHAN],
            "DELETE FROM c WHERE id = 'c2'; UPDATE c SET pool = 'gone';" + _REBUILD_P,
            "c id='c1' pool='gone' -> p",
            id="broken reference moved to another row",
        ),
        pytest.param(
            [
                "CREATE TABLE w (pool TEXT PRIMARY KEY REFERENCES p (name))"
                " WITHOUT ROWID",
                "INSERT INTO w VALUES ('gone')",
            ],
            _REBUILD_P,
            "w -> p (WITHOUT ROWID: row unknown)",
            id="broken reference that names no row",
        ),
    ],
)
async def test_a_rebuild_that_breaks_a_reference_rolls_back(
    tmp_path: Path, left_behind: list[str], sql: str, added: str
) -> None:
    """The foreign-keys-off rebuild still refuses to commit a dangling reference
    it cannot show was there before."""
    path = tmp_path / "argus.db"
    await _leave_behind(path, left_behind)
    before = await _contents(path)
    with pytest.raises(MigrationError, match=r"0002_b\.sql: .*" + re.escape(added)):
        await Database.open(path, migrations=_rebuild(sql))
    assert await _user_version(path) == 1
    assert await _contents(path) == before
    db = await Database.open(path, migrations=[_PARENT_CHILD])
    async with db.write() as tx, tx.conn.execute("PRAGMA foreign_keys") as cur:
        row = await cur.fetchone()  # the writer's, which the runner turned off
    await db.close()
    assert row is not None and row[0] == 1


async def test_0075_keeps_pools_and_accepts_the_builtin_pair(tmp_path: Path) -> None:
    path, migrations = tmp_path / "argus.db", load_migrations()
    t0 = "2026-11-01T00:00:00+00:00"
    db = await Database.open(path, migrations=migrations[:74])
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO pools (name, target, runtime, image, capabilities,"
            " agents_per_instance, desired_replicas, max_replicas, config, status,"
            " last_error, created_at, updated_at, system, oom_streak)"
            " VALUES ('orchestrator', 'docker', 'nanoclaw', 'host:1', '[\"py\"]', 1,"
            " 1, 1, '{\"env\": {}}', 'failing', 'boom', ?, ?, 'orchestrator', 2)",
            (t0, t0),
        )
        await tx.conn.execute(
            "INSERT INTO instances (id, pool, provider_ref, state, created_at)"
            " VALUES ('i1', 'orchestrator', 'argus-i1', 'running', ?)",
            (t0,),
        )
    async with db.read() as conn, conn.execute("SELECT * FROM pools") as cur:
        before = [dict(r) for r in await cur.fetchall()]
    await db.close()

    db = await Database.open(path, migrations=migrations)
    try:
        async with db.read() as conn:
            async with conn.execute("SELECT * FROM pools") as cur:
                assert [dict(r) for r in await cur.fetchall()] == before
            async with conn.execute("PRAGMA foreign_key_check") as cur:
                assert await cur.fetchall() == []
        async with db.write() as tx:
            await tx.conn.execute(
                "INSERT INTO pools (name, target, runtime, image, capabilities,"
                " max_replicas, config, created_at, updated_at)"
                " VALUES ('b', 'inprocess', 'builtin', 'builtin', '[]', 1, '{}', ?, ?)",
                (t0, t0),
            )
        for target, runtime in (("inprocess", "plain"), ("docker", "builtin")):
            with pytest.raises(aiosqlite.IntegrityError):
                async with db.write() as tx:
                    await tx.conn.execute(
                        "INSERT INTO pools (name, target, runtime, image, capabilities,"
                        " max_replicas, config, created_at, updated_at)"
                        " VALUES ('x', ?, ?, 'i', '[]', 1, '{}', ?, ?)",
                        (target, runtime, t0, t0),
                    )
    finally:
        await db.close()
