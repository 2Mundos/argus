"""Task complexity: classification, create/edit, and the soft local preference of the
lease ranking (docs/specs/task-complexity.spec.md)."""

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
from argus_core.complexity import Complexity
from argus_core.task_edit import TaskChanges
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from test_local_harness_routing import RUNTIME, add_cloud, add_local
from test_mcp import ADMIN_TOKEN, tool

client = test_mcp.client  # the same app fixture as the MCP tests

A, B = "agent-a", "agent-b"
TRIVIAL, STANDARD = Complexity.TRIVIAL, Complexity.STANDARD


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
    assert (task.complexity, task.complexity_source) == (STANDARD, "heuristic")


async def test_heuristic_marks_a_clear_copy_change_trivial(db: Database) -> None:
    task = await create(db, title="Corrigir erro de digitação no rodapé")
    assert (task.complexity, task.complexity_source) == (TRIVIAL, "heuristic")


async def test_explicit_value_always_wins(db: Database) -> None:
    standard = await create(db, title="Fix typo", complexity=STANDARD)
    assert (standard.complexity, standard.complexity_source) == (STANDARD, "explicit")
    trivial = await create(db, title="Refactor the API", complexity=TRIVIAL)
    assert (trivial.complexity, trivial.complexity_source) == (TRIVIAL, "explicit")


async def test_edit_sets_complexity_and_marks_it_explicit(db: Database) -> None:
    task = await create(db, title="Add retries")
    async with db.write() as tx:
        edited = await tasks.edit_task(
            tx,
            task.id,
            TaskChanges(complexity=TRIVIAL),
            task.version,
            "admin:test",
        )
    assert (edited.complexity, edited.complexity_source) == (TRIVIAL, "explicit")
    async with db.read() as conn:
        events = [e async for e in _events(conn, task.id)]
    assert {"from": "standard", "to": "trivial"} in [
        e.get("changes", {}).get("complexity") for e in events
    ]


async def _events(conn, task_id):
    async with conn.execute(
        "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.edited'",
        (task_id,),
    ) as cur:
        for (payload,) in await cur.fetchall():
            yield json.loads(payload)


async def test_complexity_cannot_be_cleared_or_edited_once_claimed(
    db: Database, agents
) -> None:
    task = await create(db, title="Add retries")
    async with db.write() as tx:
        with pytest.raises(ValidationFailedError):
            await tasks.edit_task(
                tx,
                task.id,
                TaskChanges.model_validate({"complexity": None}),
                task.version,
                "admin:test",
            )
    claimed = await claim(db)
    async with db.write() as tx:
        with pytest.raises(ConflictError):
            await tasks.edit_task(
                tx,
                claimed.id,
                TaskChanges(complexity=TRIVIAL),
                claimed.version,
                "admin:test",
            )


def test_rest_and_mcp_create_and_edit(client: TestClient) -> None:
    auth = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    res = client.post(
        "/api/tasks",
        json={"project": "global", "title": "t", "complexity": "trivial"},
        headers=auth,
    )
    assert res.status_code == 201
    assert res.json()["complexity"] == "trivial"
    assert res.json()["complexity_source"] == "explicit"
    bad = client.post(
        "/api/tasks",
        json={"project": "global", "title": "t", "complexity": "easy"},
        headers=auth,
    )
    assert bad.status_code == 422
    made = tool(
        client,
        ADMIN_TOKEN,
        "create_task",
        title="t",
        project="global",
        complexity="trivial",
    )
    assert made["complexity"] == "trivial"
    plain = tool(client, ADMIN_TOKEN, "create_task", title="Add auth", project="global")
    assert (plain["complexity"], plain["complexity_source"]) == (
        "standard",
        "heuristic",
    )
    edited = tool(
        client,
        ADMIN_TOKEN,
        "edit_task",
        task_id=plain["id"],
        expected_version=plain["version"],
        changes={"complexity": "trivial"},
    )
    assert edited["complexity"] == "trivial"
    got = client.get(f"/api/tasks/{plain['id']}", headers=auth).json()
    assert got["complexity"] == "trivial"
    assert got["complexity_source"] == "explicit"
    refused = client.patch(
        f"/api/tasks/{plain['id']}",
        json={"expected_version": got["version"], "changes": {"complexity": "nope"}},
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
    await create(db, title="t", complexity=TRIVIAL)
    task = await claim(db)
    got = await lease(db, store, task)
    assert got.provider == "local"


async def test_standard_task_keeps_cloud_first(db: Database, store, agents) -> None:
    await pool(db, store)
    await create(db, title="t", complexity=STANDARD)
    got = await lease(db, store, await claim(db))
    assert got.provider == "anthropic"


async def test_trivial_falls_back_to_cloud_when_local_is_full(
    db: Database, store, agents
) -> None:
    await pool(db, store)
    await create(db, title="holder", complexity=TRIVIAL)
    first = await claim(db, A)
    assert (await lease(db, store, first, A)).provider == "local"
    await create(db, title="next", complexity=TRIVIAL)
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
    await create(db, title="t", complexity=TRIVIAL)
    got = await lease(db, store, await claim(db))
    assert got.provider == "anthropic"


async def test_trivial_with_no_cloud_still_uses_local(
    db: Database, store, agents
) -> None:
    await add_local(db, store, "strata")
    await create(db, title="t", complexity=TRIVIAL)
    assert (await lease(db, store, await claim(db))).provider == "local"


async def test_lease_checks_still_apply_to_trivial_tasks(
    db: Database, store, agents
) -> None:
    await pool(db, store)
    await create(db, title="t", complexity=TRIVIAL)
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
    await create(db, title="holder", complexity=TRIVIAL)
    holder = await claim(db, A)
    held = await lease(db, store, holder, A)
    assert held.provider == "local"
    await create(db, title="next", complexity=TRIVIAL)
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
