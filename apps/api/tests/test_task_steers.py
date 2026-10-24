"""Operator steers (docs/specs/task-steering.spec.md)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.agents import service as agents
from argus_api.db import Database
from argus_api.errors import ConflictError, NotFoundError, RateLimitedError
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_api.tasks import steers
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

A, B = "agent-a", "agent-b"
ADMIN_TOKEN = "test-admin-token-" + "s" * 32


async def create(db: Database, **kw):
    async with db.write() as tx:
        return await tasks.create_task(
            tx, TaskCreate(project="global", title="t", **kw), "admin:test"
        )


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task


async def allow(db: Database, agent: str = A) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE agents SET runtime = ? WHERE id = ?",
            ('{"features": ["steer"]}', agent),
        )


async def steer(db: Database, task_id: str, message: str = "look at the tests"):
    async with db.write() as tx:
        return await steers.create(tx, task_id, message, "admin:test")


async def states(db: Database, task_id: str) -> list[str]:
    page = await _page(db, task_id)
    return [item.state for item in page.items]


async def _page(db: Database, task_id: str, attempt: int | None = None):
    async with db.read() as conn:
        return await steers.list_steers(conn, task_id, attempt)


async def kinds(db: Database, task_id: str) -> list[str]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT kind FROM events WHERE subject_id = ? ORDER BY id", (task_id,)
        ) as cur,
    ):
        return [row["kind"] for row in await cur.fetchall()]


@pytest.fixture
async def claimed(db: Database):
    await insert_agent(db, A)
    await insert_agent(db, B)
    await create(db, max_attempts=6)
    task = await claim(db)
    await allow(db)
    return task


async def test_a_steer_is_pending_until_the_holder_acks_it(
    db: Database, claimed
) -> None:
    made = await steer(db, claimed.id, "stop writing the migration")
    assert made.state == "pending" and made.attempt == claimed.attempt
    assert "task.steered" in await kinds(db, claimed.id)
    async with db.write() as tx:
        beat = await agents.heartbeat(
            tx, A, [(claimed.id, claimed.attempt), ("missing", 1)]
        )
    assert [s.id for s in beat.steers] == [made.id]
    assert beat.steers[0].message == "stop writing the migration"
    assert len(beat.rejected) == 1 and beat.rejected[0].task_id == "missing"
    async with db.write() as tx:
        again = await agents.heartbeat(
            tx, A, [], steer_acks=[made.id, made.id, "no-such-steer"]
        )
    assert again.steers == []
    assert (await states(db, claimed.id)) == ["delivered"]
    assert (await kinds(db, claimed.id)).count("task.steer_delivered") == 1
    async with db.write() as tx:
        await agents.heartbeat(
            tx, A, [(claimed.id, claimed.attempt)], steer_acks=[made.id]
        )
    assert (await kinds(db, claimed.id)).count("task.steer_delivered") == 1


async def test_a_rejected_lease_receives_no_steer(db: Database, claimed) -> None:
    made = await steer(db, claimed.id)
    async with db.write() as tx:
        beat = await agents.heartbeat(tx, A, [("missing", 1)])
    assert beat.renewed == [] and beat.steers == []
    assert (await states(db, claimed.id)) == ["pending"]
    assert made.id


async def test_another_agent_cannot_ack(db: Database, claimed) -> None:
    made = await steer(db, claimed.id)
    async with db.write() as tx:
        await agents.heartbeat(tx, B, [], steer_acks=[made.id])
    assert (await states(db, claimed.id)) == ["pending"]


async def test_refuses_when_not_claimed_or_unsupported(db: Database) -> None:
    await insert_agent(db, A)
    task = await create(db)
    with pytest.raises(ConflictError) as err:
        await steer(db, task.id)
    assert err.value.code == "not_claimed"
    claimed = await claim(db)
    with pytest.raises(ConflictError) as err:
        await steer(db, claimed.id)
    assert err.value.code == "steer_unsupported"
    with pytest.raises(NotFoundError):
        await steer(db, "missing")


async def test_pending_cap_and_rate(db: Database, claimed, clock: FakeClock) -> None:
    for i in range(steers.MAX_PENDING):
        clock.advance(1)
        await steer(db, claimed.id, f"note {i}")
    with pytest.raises(ConflictError) as err:
        await steer(db, claimed.id, "one more")
    assert err.value.code == "too_many_pending"
    async with db.write() as tx:
        await steers.cancel(
            tx, claimed.id, (await _page(db, claimed.id)).items[0].id, "admin:test"
        )
    with pytest.raises(RateLimitedError):
        await steer(db, claimed.id, "too soon")
    clock.advance(1)
    await steer(db, claimed.id, "after the wait")


async def test_only_a_pending_steer_can_be_cancelled(db: Database, claimed) -> None:
    made = await steer(db, claimed.id)
    async with db.write() as tx:
        await steers.cancel(tx, claimed.id, made.id, "admin:test")
    assert (await states(db, claimed.id)) == ["cancelled"]
    assert "task.steer_cancelled" in await kinds(db, claimed.id)
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await steers.cancel(tx, claimed.id, made.id, "admin:test")
    assert err.value.code == "not_pending"
    async with db.write() as tx:
        await agents.heartbeat(
            tx, A, [(claimed.id, claimed.attempt)], steer_acks=[made.id]
        )
    assert (await states(db, claimed.id)) == ["cancelled"]


async def test_preview_is_capped(db: Database, claimed) -> None:
    text = "x" * 250
    await steer(db, claimed.id, text)
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = 'task.steered'",
            (claimed.id,),
        ) as cur,
    ):
        data = __import__("json").loads((await cur.fetchone())["data"])
    assert data["chars"] == 250 and data["preview"] == "x" * 200


@pytest.mark.parametrize(
    "ender", ["complete", "fail", "block", "release", "cancel", "expire"]
)
async def test_leaving_claimed_expires_pending_steers(
    db: Database, claimed, clock: FakeClock, ender: str
) -> None:
    await steer(db, claimed.id)
    if ender == "expire":
        # `tx.now` is fixed when the write starts, so the lease must already be past.
        clock.advance(claimed.lease_ttl_s + 1)
    async with db.write() as tx:
        if ender == "complete":
            await tasks.complete(tx, A, claimed.id, claimed.attempt, DONE)
        elif ender == "fail":
            await tasks.fail(tx, A, claimed.id, claimed.attempt, "nope")
        elif ender == "block":
            await tasks.block(
                tx, A, claimed.id, claimed.attempt, "waiting for decision"
            )
        elif ender == "release":
            await tasks.release(tx, claimed.id, "admin:test", "operator")
        elif ender == "cancel":
            await tasks.cancel(tx, claimed.id, "admin:test")
        else:
            await tasks.expire_leases(tx)
    assert (await states(db, claimed.id)) == ["expired"]
    page = await _page(db, claimed.id)
    assert page.items[0].closed_at == clock.now


async def test_retry_expires_a_leftover_pending_steer(db: Database, claimed) -> None:
    await steer(db, claimed.id)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET max_attempts = 1 WHERE id = ?", (claimed.id,)
        )
        await tasks.fail(tx, A, claimed.id, claimed.attempt, "nope")
        await tx.conn.execute(
            "UPDATE task_steers SET state = 'pending', closed_at = NULL WHERE task_id = ?",
            (claimed.id,),
        )
        await tasks.retry(tx, claimed.id, "admin:test")
    page = await _page(db, claimed.id, claimed.attempt)
    assert [item.state for item in page.items] == ["expired"]


# -- REST ------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def register(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_rest_contract(client: TestClient) -> None:
    worker = register(client)
    task = client.post("/api/tasks", json={"title": "t", "project": "global"}).json()
    url = f"/api/tasks/{task['id']}/steers"
    assert client.post(url, json={"message": "hi"}, headers=worker).status_code == 403
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    refused = client.post(url, json={"message": "hi"})
    assert (
        refused.status_code == 409
        and refused.json()["detail"]["code"] == "steer_unsupported"
    )
    assert client.post(url, json={"message": "   "}).status_code == 422
    beat = client.post(
        "/api/agents/me/heartbeat",
        json={
            "leases": [{"task_id": claimed["id"], "attempt": claimed["attempt"]}],
            "runtime": {"features": ["steer"]},
        },
        headers=worker,
    )
    assert beat.status_code == 200, beat.text
    made = client.post(url, json={"message": "  check the gate  "})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["state"] == "pending" and body["message"] == "check the gate"
    assert client.post(url, json={"message": "again"}).status_code == 429
    listed = client.get(url).json()
    assert listed["attempt"] == claimed["attempt"]
    assert [item["id"] for item in listed["items"]] == [body["id"]]
    assert client.get(f"{url}?attempt={2**63}").status_code == 422
    assert client.get(f"{url}?attempt={2**63 - 1}").status_code == 200
    shown = client.post(
        "/api/agents/me/heartbeat",
        json={"leases": [{"task_id": claimed["id"], "attempt": claimed["attempt"]}]},
        headers=worker,
    )
    assert shown.status_code == 200, shown.text
    assert [s["id"] for s in shown.json()["steers"]] == [body["id"]]
    delivered = client.post(
        "/api/agents/me/heartbeat",
        json={
            "leases": [{"task_id": claimed["id"], "attempt": claimed["attempt"]}],
            "steer_acks": [body["id"]],
        },
        headers=worker,
    )
    assert delivered.status_code == 200 and delivered.json()["steers"] == []
    assert (
        client.delete(f"{url}/{body['id']}").json()["detail"]["code"] == "not_pending"
    )
    assert "task.steered" in json_kinds(client, task["id"])


def json_kinds(client: TestClient, task_id: str) -> list[str]:
    events = client.get(f"/api/events?subject_id={task_id}&limit=50").json()
    return [event["kind"] for event in events]
