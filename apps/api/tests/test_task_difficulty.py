"""Task difficulty: classification, create/edit, and the soft local preference of the
lease ranking (docs/specs/task-difficulty.spec.md)."""

import json
from datetime import timedelta

import pytest
import test_mcp
from api_support import insert_agent
from argus_api.accounts import service as accounts
from argus_api.db import Database
from argus_api.errors import (
    ConflictError,
    LeaseConflictError,
    ValidationFailedError,
)
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountUpdate, ExhaustedReport
from argus_core.difficulty import Difficulty
from argus_core.task_edit import TaskChanges
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from test_local_harness_routing import RUNTIME, add_cloud, add_local
from test_mcp import ADMIN_TOKEN, tool

client = test_mcp.client  # the same app fixture as the MCP tests

A, B = "agent-a", "agent-b"
TRIVIAL, STANDARD, HARD = Difficulty.TRIVIAL, Difficulty.STANDARD, Difficulty.HARD


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
async def agents(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx,
            TaskCreate(**({"project": "global", "title": "t"} | kw)),
            "admin:test",
        )


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task


async def lease(db: Database, store, task, agent: str = A):
    async with db.write() as tx:
        return await accounts.lease(
            tx, agent, RUNTIME, store, task_id=task.id, attempt=task.attempt
        )


# -- create and edit -------------------------------------------------------------------


async def test_default_is_standard_by_heuristic(db: Database) -> None:
    task = await create(db, title="Add retries to the worker")
    assert (task.difficulty, task.difficulty_source) == (STANDARD, "inferred")


async def test_heuristic_marks_a_clear_copy_change_trivial(db: Database) -> None:
    task = await create(db, title="Fix typo in the footer")
    assert (task.difficulty, task.difficulty_source) == (TRIVIAL, "inferred")


async def test_explicit_value_always_wins(db: Database) -> None:
    standard = await create(db, title="Fix typo", difficulty=STANDARD)
    assert (standard.difficulty, standard.difficulty_source) == (STANDARD, "declared")
    trivial = await create(db, title="Refactor the API", difficulty=TRIVIAL)
    assert (trivial.difficulty, trivial.difficulty_source) == (TRIVIAL, "declared")


async def test_edit_sets_difficulty_and_marks_it_explicit(db: Database) -> None:
    task = await create(db, title="Add retries")
    async with db.write() as tx:
        edited = await tasks.edit_task(
            tx,
            task.id,
            TaskChanges(difficulty=TRIVIAL),
            task.version,
            "admin:test",
        )
    assert (edited.difficulty, edited.difficulty_source) == (TRIVIAL, "declared")
    async with db.read() as conn:
        events = [e async for e in _events(conn, task.id)]
    assert {"from": "standard", "to": "trivial"} in [
        e.get("changes", {}).get("difficulty") for e in events
    ]


async def _events(conn, task_id):
    async with conn.execute(
        "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.edited'",
        (task_id,),
    ) as cur:
        for (payload,) in await cur.fetchall():
            yield json.loads(payload)


async def test_difficulty_cannot_be_cleared_or_edited_once_claimed(
    db: Database, agents
) -> None:
    task = await create(db, title="Add retries")
    async with db.write() as tx:
        with pytest.raises(ValidationFailedError):
            await tasks.edit_task(
                tx,
                task.id,
                TaskChanges.model_validate({"difficulty": None}),
                task.version,
                "admin:test",
            )
    claimed = await claim(db)
    async with db.write() as tx:
        with pytest.raises(ConflictError):
            await tasks.edit_task(
                tx,
                claimed.id,
                TaskChanges(difficulty=TRIVIAL),
                claimed.version,
                "admin:test",
            )


def test_rest_and_mcp_create_and_edit(client: TestClient) -> None:
    auth = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    res = client.post(
        "/api/tasks",
        json={"project": "global", "title": "t", "difficulty": "trivial"},
        headers=auth,
    )
    assert res.status_code == 201
    assert res.json()["difficulty"] == "trivial"
    assert res.json()["difficulty_source"] == "declared"
    bad = client.post(
        "/api/tasks",
        json={"project": "global", "title": "t", "difficulty": "easy"},
        headers=auth,
    )
    assert bad.status_code == 422
    made = tool(
        client,
        ADMIN_TOKEN,
        "create_task",
        title="t",
        project="global",
        difficulty="trivial",
    )
    assert made["difficulty"] == "trivial"
    plain = tool(client, ADMIN_TOKEN, "create_task", title="Add auth", project="global")
    assert (plain["difficulty"], plain["difficulty_source"]) == (
        "standard",
        "inferred",
    )
    edited = tool(
        client,
        ADMIN_TOKEN,
        "edit_task",
        task_id=plain["id"],
        expected_version=plain["version"],
        changes={"difficulty": "trivial"},
    )
    assert edited["difficulty"] == "trivial"
    got = client.get(f"/api/tasks/{plain['id']}", headers=auth).json()
    assert got["difficulty"] == "trivial"
    assert got["difficulty_source"] == "declared"
    refused = client.patch(
        f"/api/tasks/{plain['id']}",
        json={"expected_version": got["version"], "changes": {"difficulty": "nope"}},
        headers=auth,
    )
    assert refused.status_code == 422


# -- lease ranking ---------------------------------------------------------------------


async def pool(db: Database, store) -> tuple[str, str]:
    cloud = await add_cloud(db, store, "claude", "anthropic", priority=100)
    local = await add_local(db, store, "strata", priority=-10)
    return cloud, local


async def test_trivial_task_leases_local_first(db: Database, store, agents) -> None:
    await pool(db, store)
    await create(db, title="t", difficulty=TRIVIAL)
    task = await claim(db)
    got = await lease(db, store, task)
    assert got.provider == "local"


async def test_standard_task_keeps_cloud_first(db: Database, store, agents) -> None:
    await pool(db, store)
    await create(db, title="t", difficulty=STANDARD)
    got = await lease(db, store, await claim(db))
    assert got.provider == "anthropic"


async def test_trivial_falls_back_to_cloud_when_local_is_full(
    db: Database, store, agents
) -> None:
    await pool(db, store)
    await create(db, title="holder", difficulty=TRIVIAL)
    first = await claim(db, A)
    assert (await lease(db, store, first, A)).provider == "local"
    await create(db, title="next", difficulty=TRIVIAL)
    second = await claim(db, B)
    got = await lease(db, store, second, B)  # one call, no wait
    assert got.provider == "anthropic"
    async with db.read() as conn:
        task = await tasks.get_task(conn, second.id)
    assert task.state == "claimed"
    assert task.attempt == second.attempt == 1
    assert task.failures == 0


async def test_trivial_falls_back_to_cloud_when_local_is_down(
    db: Database, store, agents
) -> None:
    _, local = await pool(db, store)
    async with db.write() as tx:
        await accounts.update_account(
            tx, local, AccountUpdate(enabled=False), "admin:test"
        )
    await create(db, title="t", difficulty=TRIVIAL)
    got = await lease(db, store, await claim(db))
    assert got.provider == "anthropic"


async def test_trivial_with_no_cloud_still_uses_local(
    db: Database, store, agents
) -> None:
    await add_local(db, store, "strata")
    await create(db, title="t", difficulty=TRIVIAL)
    assert (await lease(db, store, await claim(db))).provider == "local"


async def test_lease_checks_still_apply_to_trivial_tasks(
    db: Database, store, agents
) -> None:
    await pool(db, store)
    await create(db, title="t", difficulty=TRIVIAL)
    task = await claim(db)
    async with db.write() as tx:
        with pytest.raises(LeaseConflictError):
            await accounts.lease(
                tx, A, RUNTIME, store, task_id=task.id, attempt=task.attempt + 1
            )
    async with db.write() as tx:
        with pytest.raises(LeaseConflictError):
            await accounts.lease(
                tx, B, RUNTIME, store, task_id=task.id, attempt=task.attempt
            )


async def test_failover_after_exhaustion_also_prefers_local_for_trivial(
    db: Database, store, agents, clock
) -> None:
    cloud, _ = await pool(db, store)
    await create(db, title="holder", difficulty=TRIVIAL)
    holder = await claim(db, A)
    held = await lease(db, store, holder, A)
    assert held.provider == "local"
    await create(db, title="next", difficulty=TRIVIAL)
    task = await claim(db, B)
    assert (await lease(db, store, task, B)).provider == "anthropic"
    async with db.write() as tx:  # the local slot frees up
        await accounts.release(tx, A, "released", "engine")
    async with db.write() as tx:
        result = await accounts.exhausted(
            tx,
            B,
            cloud,
            ExhaustedReport(
                until=clock.now + timedelta(hours=1),
                reason="quota",
                runtime_providers=RUNTIME,
                task_id=task.id,
                attempt=task.attempt,
            ),
            store,
        )
    assert result.next_provider == "local"


# -- hard ------------------------------------------------------------------------------


async def test_hard_is_set_explicitly_or_by_two_hard_signals(db: Database) -> None:
    explicit = await create(db, title="Add retries", difficulty=HARD)
    assert (explicit.difficulty, explicit.difficulty_source) == (HARD, "declared")
    guessed = await create(db, title="Data migration across multi-service accounts")
    assert (guessed.difficulty, guessed.difficulty_source) == (HARD, "inferred")
    one = await create(db, title="Improve login security")
    assert one.difficulty == STANDARD


async def test_edit_to_hard(db: Database) -> None:
    task = await create(db, title="Add retries")
    async with db.write() as tx:
        edited = await tasks.edit_task(
            tx, task.id, TaskChanges(difficulty=HARD), task.version, "admin:test"
        )
    assert (edited.difficulty, edited.difficulty_source) == (HARD, "declared")


async def test_hard_task_keeps_cloud_first(db: Database, store, agents) -> None:
    await pool(db, store)
    await create(db, title="Add retries", difficulty=HARD)
    held = await lease(db, store, await claim(db))
    assert held.provider == "anthropic"


async def test_the_column_rejects_unknown_values(db: Database) -> None:
    task = await create(db, title="Add retries", difficulty=TRIVIAL)
    async with db.write() as tx:
        with pytest.raises(Exception, match="CHECK"):
            await tx.conn.execute(
                "UPDATE tasks SET difficulty = 'epic' WHERE id = ?", (task.id,)
            )


def test_migration_0036_maps_complexity_to_difficulty() -> None:
    import sqlite3
    from pathlib import Path

    import argus_api.db as db_pkg

    migrations = Path(db_pkg.__file__).parent / "migrations"
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    conn.executescript((migrations / "0032_task_complexity.sql").read_text())
    conn.executescript((migrations / "0033_task_complexity_hard.sql").read_text())
    conn.execute(
        "INSERT INTO tasks (id, complexity, complexity_source) VALUES"
        " ('a', 'trivial', 'heuristic'), ('b', 'standard', 'explicit'),"
        " ('c', 'hard', 'explicit'), ('d', 'standard', 'heuristic')"
    )
    conn.executescript((migrations / "0036_task_difficulty.sql").read_text())
    rows = conn.execute(
        "SELECT id, difficulty, difficulty_source FROM tasks ORDER BY id"
    )
    assert rows.fetchall() == [
        ("a", "trivial", "inferred"),
        ("b", "standard", "declared"),
        ("c", "hard", "declared"),
        ("d", "standard", "inferred"),
    ]
    conn.execute("INSERT INTO tasks (id, difficulty) VALUES ('e', 'simple')")
    conn.execute("INSERT INTO tasks (id) VALUES ('f')")  # a row written by old code
    assert conn.execute(
        "SELECT difficulty, difficulty_source FROM tasks WHERE id = 'f'"
    ).fetchone() == ("standard", "inferred")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tasks (id, difficulty) VALUES ('g', 'epic')")


# -- escalation --------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["context_overflow", "verification_failed"])
async def test_overflow_and_failed_verification_raise_difficulty_one_level(
    db: Database, agents, reason: str
) -> None:
    task = await create(db, title="Rewrite the engine", difficulty="simple")
    claimed = await claim(db)
    async with db.write() as tx:
        after = await tasks.fail(tx, A, claimed.id, claimed.attempt, "too big", reason)
    assert (after.difficulty, after.difficulty_source) == ("standard", "inferred")
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.fail'",
            (task.id,),
        ) as cur,
    ):
        rows = [json.loads(r[0]) for r in await cur.fetchall()]
    assert rows[-1]["difficulty_escalated"] == {
        "from": "simple",
        "to": "standard",
        "reason": reason,
    }


async def test_an_ordinary_failure_keeps_the_difficulty(db: Database, agents) -> None:
    await create(db, title="Rewrite the engine", difficulty="simple")
    claimed = await claim(db)
    async with db.write() as tx:
        after = await tasks.fail(tx, A, claimed.id, claimed.attempt, "oops")
    assert after.difficulty == "simple"


async def test_hard_cannot_escalate_further(db: Database, agents) -> None:
    await create(db, title="Rewrite the engine", difficulty="hard")
    claimed = await claim(db)
    async with db.write() as tx:
        after = await tasks.fail(
            tx, A, claimed.id, claimed.attempt, "x", "context_overflow"
        )
    assert (after.difficulty, after.difficulty_source) == ("hard", "declared")
