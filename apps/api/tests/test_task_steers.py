"""Operator steers (docs/specs/task-steering.spec.md)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api.agents import service as agents
from argus_api.db import Database, ts
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


async def beat(db: Database, task, agent: str = A, acks: list[str] | None = None):
    async with db.write() as tx:
        return await agents.heartbeat(
            tx, agent, [(task.id, task.attempt)], steer_acks=acks
        )


async def column(db: Database, steer_id: str, name: str) -> str | None:
    async with (
        db.read() as conn,
        conn.execute(
            f"SELECT {name} FROM task_steers WHERE id = ?",
            (steer_id,),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def attempt_unknown(db: Database, task_id: str) -> list[int]:
    async with db.read() as conn:
        return [a.steer_unknown for a in await tasks.attempt_definitions(conn, task_id)]


async def test_first_dispatch_is_recorded_once_and_survives_a_lost_answer(
    db: Database, claimed, clock: FakeClock
) -> None:
    made = await steer(db, claimed.id)
    assert await column(db, made.id, "dispatched_at") is None
    first = await beat(db, claimed)  # the answer is lost on the way back
    assert [s.id for s in first.steers] == [made.id]
    dispatched = await column(db, made.id, "dispatched_at")
    assert dispatched is not None
    first_at = clock.now
    clock.advance(30)
    again = await beat(db, claimed)  # still pending: carried again, same dispatch
    assert [s.id for s in again.steers] == [made.id]
    assert await column(db, made.id, "dispatched_at") == dispatched
    page = await _page(db, claimed.id)
    assert page.items[0].state == "pending"
    assert page.items[0].dispatched_at == first_at
    await beat(db, claimed, acks=[made.id])
    assert (await states(db, claimed.id)) == ["delivered"]


async def test_a_lost_ack_keeps_the_steer_listed_within_the_attempt(
    db: Database, claimed, clock: FakeClock
) -> None:
    made = await steer(db, claimed.id)
    await beat(db, claimed)
    # The worker injected it; the heartbeat carrying the ack never arrived.
    clock.advance(30)
    retried = await beat(db, claimed)
    assert [s.id for s in retried.steers] == [made.id]
    assert (await states(db, claimed.id)) == ["pending"]
    clock.advance(30)
    await beat(db, claimed, acks=[made.id])
    assert (await states(db, claimed.id)) == ["delivered"]
    assert (await kinds(db, claimed.id)).count("task.steer_delivered") == 1


@pytest.mark.parametrize("lease", ["foreign", "stale", "missing"])
async def test_a_rejected_lease_dispatches_nothing(
    db: Database, claimed, lease: str
) -> None:
    made = await steer(db, claimed.id)
    held = {
        "foreign": (B, (claimed.id, claimed.attempt)),
        "stale": (A, (claimed.id, claimed.attempt + 1)),
        "missing": (A, ("missing", 1)),
    }[lease]
    async with db.write() as tx:
        answer = await agents.heartbeat(tx, held[0], [held[1]])
    assert answer.renewed == [] and answer.steers == []
    assert await column(db, made.id, "dispatched_at") is None
    async with db.write() as tx:
        await tasks.cancel(tx, claimed.id, "admin:test")
    assert (await states(db, claimed.id)) == ["expired"]


# Spec 3.3 also lists reassignment. There is no dedicated reassignment API: the attempt
# ends in steers.close_attempt when the lease expires and another worker claims (see
# test_reassignment_closes_dispatched_steer_as_unknown), or via the same close_attempt
# hook as the other leave-claimed paths below.

ENDERS = [
    "complete",
    "fail",
    "fail_final",
    "block",
    "requeue",
    "release",
    "cancel",
    "lease_expiry",
    "timeout",
    "agent_left",
]


async def _end(db: Database, task, clock: FakeClock, ender: str) -> None:
    if ender == "lease_expiry":
        # `tx.now` is fixed when the write starts, so the lease must already be past.
        clock.advance(task.lease_ttl_s + 1)
    async with db.write() as tx:
        if ender == "complete":
            await tasks.complete(tx, A, task.id, task.attempt, DONE)
        elif ender == "fail":
            await tasks.fail(tx, A, task.id, task.attempt, "nope")
        elif ender == "fail_final":
            await tx.conn.execute(
                "UPDATE tasks SET max_attempts = 1 WHERE id = ?", (task.id,)
            )
            await tasks.fail(tx, A, task.id, task.attempt, "nope")
        elif ender == "block":
            await tasks.block(tx, A, task.id, task.attempt, "waiting for decision")
        elif ender == "requeue":
            await tasks.pause_for_quota(
                tx, A, task.id, task.attempt, "quota", wait=False
            )
        elif ender == "release":
            await tasks.release(tx, task.id, "admin:test", "operator")
        elif ender == "cancel":
            await tasks.cancel(tx, task.id, "admin:test")
        elif ender == "lease_expiry":
            await tasks.expire_leases(tx)
        elif ender == "timeout":
            await tx.conn.execute(
                "UPDATE tasks SET timeout_at = ? WHERE id = ?",
                (ts(tx.now), task.id),
            )
            await tasks.expire_timeouts(tx)
        else:
            await agents.leave(tx, A)


async def test_reassignment_closes_dispatched_steer_as_unknown(
    db: Database, claimed, clock: FakeClock
) -> None:
    await steer(db, claimed.id, "before reassignment")
    await beat(db, claimed)
    clock.advance(claimed.lease_ttl_s + 1)
    async with db.write() as tx:
        assert await tasks.expire_leases(tx) == [claimed.id]
    reassigned = await claim(db, B)
    assert reassigned.id == claimed.id and reassigned.attempt == claimed.attempt + 1
    page = await _page(db, claimed.id, claimed.attempt)
    assert [i.state for i in page.items] == ["unknown"]
    assert await attempt_unknown(db, claimed.id) == [1, 0]


@pytest.mark.parametrize("ender", ENDERS)
async def test_leaving_claimed_closes_pending_steers_by_dispatch(
    db: Database, claimed, clock: FakeClock, ender: str
) -> None:
    delivered = await steer(db, claimed.id, "delivered")
    await beat(db, claimed)
    await beat(db, claimed, acks=[delivered.id])
    clock.advance(1)
    cancelled = await steer(db, claimed.id, "cancelled")
    async with db.write() as tx:
        await steers.cancel(tx, claimed.id, cancelled.id, "admin:test")
    clock.advance(1)
    carried = await steer(db, claimed.id, "carried, never acknowledged")
    await beat(db, claimed)
    clock.advance(1)
    await steer(db, claimed.id, "never carried")
    await _end(db, claimed, clock, ender)
    page = await _page(db, claimed.id, claimed.attempt)
    assert [i.state for i in page.items] == [
        "delivered",
        "cancelled",
        "unknown",
        "expired",
    ]
    closed = {i.closed_at for i in page.items[2:]}
    assert closed == {clock.now}  # one transaction, the attempt's end
    assert page.items[0].closed_at is None and page.items[1].closed_at is not None
    assert page.items[2].delivered_at is None
    assert await attempt_unknown(db, claimed.id) == [1]
    # Terminal: never carried again, and a delayed ack cannot resurrect it.
    async with db.write() as tx:
        await steers.acknowledge(tx, A, [carried.id])
        assert await steers.pending_for(tx, [(claimed.id, claimed.attempt)]) == []
    assert (await _page(db, claimed.id, claimed.attempt)).items[2].state == "unknown"
    assert (await kinds(db, claimed.id)).count("task.steer_delivered") == 1


async def test_without_dispatch_every_steer_expires(
    db: Database, claimed, clock: FakeClock
) -> None:
    await steer(db, claimed.id)
    await _end(db, claimed, clock, "release")
    assert (await states(db, claimed.id)) == ["expired"]
    assert await attempt_unknown(db, claimed.id) == [0]


async def test_a_delayed_ack_from_the_next_attempt_is_ignored(
    db: Database, claimed, clock: FakeClock
) -> None:
    made = await steer(db, claimed.id)
    await beat(db, claimed)
    await _end(db, claimed, clock, "release")
    again = await claim(db)  # the same worker holds the next attempt
    assert again.attempt == claimed.attempt + 1
    answer = await beat(db, again, acks=[made.id])
    assert answer.steers == []
    page = await _page(db, claimed.id, claimed.attempt)
    assert [i.state for i in page.items] == ["unknown"]
    assert page.items[0].delivered_at is None


async def test_retry_closes_a_leftover_pending_steer(db: Database, claimed) -> None:
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


async def test_retry_closes_a_carried_leftover_as_unknown(
    db: Database, claimed
) -> None:
    await steer(db, claimed.id)
    await beat(db, claimed)
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
    assert (await states(db, claimed.id)) == []  # attempt 0 after the retry
    page = await _page(db, claimed.id, claimed.attempt)
    assert [item.state for item in page.items] == ["unknown"]


async def previous(db: Database, task) -> int:
    async with db.read() as conn:
        return await steers.previous_unknown(conn, task.id, task.attempt)


async def test_only_the_immediately_previous_attempt_is_reported(
    db: Database, claimed, clock: FakeClock
) -> None:
    assert await previous(db, claimed) == 0  # the first attempt has no previous one
    for text in ("one", "two"):
        await steer(db, claimed.id, text)
        clock.advance(1)
    await beat(db, claimed)
    await _end(db, claimed, clock, "release")
    second = await claim(db, B)
    assert second.id == claimed.id and second.attempt == claimed.attempt + 1
    assert await previous(db, second) == 2
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE agents SET runtime = ? WHERE id = ?",
            ('{"features": ["steer"]}', B),
        )
    # The next attempt is never handed the unknown text.
    assert (await beat(db, second, agent=B)).steers == []
    assert await _page(db, second.id, second.attempt) == steers.SteerPage(
        attempt=second.attempt
    )
    async with db.write() as tx:
        await tasks.release(tx, second.id, "admin:test", "operator")
    third = await claim(db, A)
    assert third.id == claimed.id
    other = await create(db, max_attempts=6)
    assert await previous(db, third) == 0  # attempt 1's count does not reach attempt 3
    async with db.write() as tx:
        await tasks.cancel(tx, third.id, "admin:test")
    fresh = await claim(db, B)
    assert fresh.id == other.id and await previous(db, fresh) == 0


async def test_previous_attempt_count_crosses_a_retry(
    db: Database, claimed, clock: FakeClock
) -> None:
    await steer(db, claimed.id)
    await beat(db, claimed)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET max_attempts = 1 WHERE id = ?", (claimed.id,)
        )
        await tasks.fail(tx, A, claimed.id, claimed.attempt, "nope")
        await tasks.retry(tx, claimed.id, "admin:test")
    again = await claim(db)
    assert again.attempt == 1  # numbering restarts; the previous row is still found
    assert await previous(db, again) == 1


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


def test_rest_unknown_outcome_reaches_the_next_claim(client: TestClient) -> None:
    first, second = register(client), register(client)
    task = client.post("/api/tasks", json={"title": "t", "project": "global"}).json()
    url = f"/api/tasks/{task['id']}/steers"
    claimed = client.post("/api/tasks/claim", json={}, headers=first).json()
    assert claimed["steer_unknown"] == 0
    lease = {"task_id": claimed["id"], "attempt": claimed["attempt"]}
    beat = {"leases": [lease], "runtime": {"features": ["steer"]}}
    client.post("/api/agents/me/heartbeat", json=beat, headers=first)
    made = client.post(url, json={"message": "secret direction"}).json()
    carried = client.post("/api/agents/me/heartbeat", json=beat, headers=first)
    assert [s["id"] for s in carried.json()["steers"]] == [made["id"]]
    # The ack never arrives; the operator frees the worker.
    released = client.post(
        f"/api/tasks/{task['id']}/release", json={"reason": "operator"}
    )
    assert released.status_code == 200, released.text
    item = client.get(f"{url}?attempt={claimed['attempt']}").json()["items"][0]
    assert item["state"] == "unknown"
    assert item["dispatched_at"] and item["closed_at"] and not item["delivered_at"]
    again = client.post("/api/tasks/claim", json={}, headers=second)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["id"] == task["id"] and body["steer_unknown"] == 1
    assert "secret direction" not in again.text
    shown = client.post(
        "/api/agents/me/heartbeat",
        json={
            "leases": [{"task_id": body["id"], "attempt": body["attempt"]}],
            "runtime": {"features": ["steer"]},
            "steer_acks": [made["id"]],
        },
        headers=second,
    )
    assert shown.status_code == 200 and shown.json()["steers"] == []
    attempts = client.get(f"/api/tasks/{task['id']}/attempts").json()
    assert [a["steer_unknown"] for a in attempts] == [1, 0]
    assert client.get(f"{url}?attempt={claimed['attempt']}").json() == {
        "attempt": claimed["attempt"],
        "items": [item],
    }


def test_openapi_names_the_unknown_outcome() -> None:
    schemas = app.openapi()["components"]["schemas"]
    steer = schemas["Steer"]["properties"]
    assert "unknown" in steer["state"]["enum"]
    assert "dispatched_at" in steer
    assert "steer_unknown" in schemas["ClaimedTask"]["properties"]
    assert "steer_unknown" in schemas["TaskAttempt"]["properties"]
    assert "dispatched_at" not in schemas["SteerNotice"]["properties"]
