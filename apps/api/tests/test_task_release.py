"""Operator release of a claimed task: requeued, no attempt counted
(worker lifecycle 3.3, 3.4, 3.5)."""

import json
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, FakeTarget, insert_agent
from argus_api.accounts import service as accounts
from argus_api.agents import service as agents
from argus_api.db import Database
from argus_api.errors import ConflictError, ForbiddenError, LeaseConflictError
from argus_api.fleet import service as fleet
from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
from argus_api.fleet.targets import TargetRegistry
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_core.accounts import AccountCreate, ExhaustedReport
from argus_core.agents import AgentRegister
from argus_core.fleet import PoolUpsert
from argus_core.tasks import TaskCreate
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient

A, B = "agent-a", "agent-b"


async def new_task(db: Database, **kw) -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="global", title="release me", **kw), "admin:test"
        )
    return task.id


async def claim(db: Database, agent: str) -> tuple[str, int]:
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task.id, task.attempt


async def release(db: Database, task_id: str, reason="recycle", **kw):
    async with db.write() as tx:
        return await tasks.release(tx, task_id, "admin:ops", reason, **kw)


async def events(db: Database, task_id: str, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT actor, data FROM events WHERE subject_id = ? AND kind = ?"
            " ORDER BY id",
            (task_id, kind),
        ) as cur,
    ):
        return [
            {"actor": r["actor"], **json.loads(r["data"])} for r in await cur.fetchall()
        ]


async def fail(db: Database, agent: str, task_id: str, attempt: int):
    async with db.write() as tx:
        return await tasks.fail(tx, agent, task_id, attempt, "broke", "session_lost")


# -- the release itself -------------------------------------------------------------


async def test_release_requeues_without_counting_a_failure(db: Database) -> None:
    await insert_agent(db, A)
    task_id = await new_task(db)
    _, attempt = await claim(db, A)
    task = await release(db, task_id, "rolling the pool to worker:2")
    assert task.state is S.READY
    assert (task.attempt, task.failures) == (attempt, 0)  # attempt still fences
    assert task.assignee_agent_id is None and task.lease_expires_at is None
    assert task.timeout_at is None and task.error is None
    assert task.progress_message == (
        f"attempt {attempt} released: rolling the pool to worker:2; requeued"
    )
    [event] = await events(db, task_id, "task.released")
    assert event == {
        "actor": "admin:ops",
        "from": "claimed",
        "to": "ready",
        "attempt": attempt,
        "reason": "rolling the pool to worker:2",
        "previous_holder": A,
    }
    async with db.read() as conn:
        [row] = await tasks.attempt_definitions(conn, task_id)
    assert (row.attempt, row.outcome, row.outcome_reason) == (
        attempt,
        "released",
        "rolling the pool to worker:2",
    )


async def test_a_released_last_attempt_keeps_its_attempt(db: Database) -> None:
    await insert_agent(db, A)
    task_id = await new_task(db, max_attempts=2)
    _, first = await claim(db, A)
    await fail(db, A, task_id, first)  # one failure: one attempt left
    for _ in range(3):  # however often it is released, the last attempt stays
        _, attempt = await claim(db, A)
        task = await release(db, task_id)
        assert (task.state, task.failures) == (S.READY, 1)
    _, attempt = await claim(db, A)
    assert attempt == first + 4  # monotonic: every claim fences the previous one
    task = await fail(db, A, task_id, attempt)
    assert (task.state, task.failures) == (S.FAILED, 2)


async def test_release_clears_a_stale_error(db: Database) -> None:
    await insert_agent(db, A)
    task_id = await new_task(db)
    _, attempt = await claim(db, A)
    await fail(db, A, task_id, attempt)
    await claim(db, A)
    task = await release(db, task_id)
    assert task.error is None and task.failures == 1


async def test_only_a_claimed_task_at_the_given_attempt_is_released(
    db: Database,
) -> None:
    await insert_agent(db, A)
    task_id = await new_task(db)
    with pytest.raises(ConflictError) as err:
        await release(db, task_id)
    assert err.value.code == "task_not_claimed"
    _, attempt = await claim(db, A)
    with pytest.raises(ConflictError) as err:
        await release(db, task_id, attempt=attempt + 1)
    assert err.value.code == "stale_attempt"
    assert (await release(db, task_id, attempt=attempt)).state is S.READY


async def test_the_old_holder_is_fenced_off(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)
    task_id = await new_task(db)
    _, attempt = await claim(db, A)
    await release(db, task_id)
    for write in (
        lambda tx: tasks.complete(tx, A, task_id, attempt, DONE),
        lambda tx: tasks.fail(tx, A, task_id, attempt, "late"),
        lambda tx: tasks.report_progress(tx, A, task_id, attempt, "still here"),
    ):
        with pytest.raises(LeaseConflictError) as err:
            async with db.write() as tx:
                await write(tx)
        assert err.value.code == "not_claimed"
    async with db.write() as tx:
        beat = await agents.heartbeat(tx, A, [(task_id, attempt)])
    assert [(r.task_id, r.code) for r in beat.rejected] == [(task_id, "not_claimed")]
    _, again = await claim(db, B)
    async with db.write() as tx:
        beat = await agents.heartbeat(tx, A, [(task_id, attempt)])
    assert [r.code for r in beat.rejected] == ["not_assignee"]
    async with db.read() as conn:
        task = await tasks.get_task(conn, task_id)
    assert (task.assignee_agent_id, task.attempt, task.failures) == (B, again, 0)


# -- account leases (account selection: every credential call needs the live claim) -


@pytest.fixture
def store() -> MemorySecretStore:
    return MemorySecretStore()


async def add_account(db: Database, store: MemorySecretStore, name: str) -> str:
    await store.put(name, {"api_key": f"key-{name}"})
    async with db.write() as tx:
        account = await accounts.create_account(
            tx,
            name,
            AccountCreate(name=name, provider="anthropic", secret_ref=f"sops:{name}"),
            f"sops:{name}",
            "admin:test",
            store,
        )
    return account.id


async def active_leases(db: Database, agent: str) -> list[str]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT role FROM account_leases WHERE agent_id = ? AND released_at IS NULL",
            (agent,),
        ) as cur,
    ):
        return [r["role"] for r in await cur.fetchall()]


async def test_release_ends_the_holders_account_leases(
    db: Database, store: MemorySecretStore
) -> None:
    await insert_agent(db, A)
    account_id = await add_account(db, store, "claude-1")
    task_id = await new_task(db)
    _, attempt = await claim(db, A)
    async with db.write() as tx:
        await accounts.lease(
            tx, A, ["anthropic"], store, task_id=task_id, attempt=attempt
        )
    assert await active_leases(db, A) == ["engine"]

    await release(db, task_id)
    assert await active_leases(db, A) == []
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT release_reason FROM account_leases WHERE agent_id = ?", (A,)
        ) as cur,
    ):
        assert [r[0] for r in await cur.fetchall()] == ["task_released"]
    async with db.read() as conn:  # no lease: the credential cannot be rotated
        with pytest.raises(ForbiddenError):
            await accounts.secret_name_for_update(conn, account_id, A)
    with pytest.raises(LeaseConflictError):  # nor leased again for that attempt
        async with db.write() as tx:
            await accounts.lease(
                tx, A, ["anthropic"], store, task_id=task_id, attempt=attempt
            )
    with pytest.raises(ConflictError):  # nor reported exhausted (409, no lease)
        async with db.write() as tx:
            await accounts.exhausted(
                tx,
                A,
                account_id,
                ExhaustedReport(
                    task_id=task_id,
                    attempt=attempt,
                    reason="rate limit",
                    runtime_providers=["anthropic"],
                ),
                store,
            )


async def test_leases_serving_another_claim_are_kept(
    db: Database, store: MemorySecretStore
) -> None:
    await insert_agent(db, A)
    await add_account(db, store, "claude-1")
    first = await new_task(db, priority=1)
    await new_task(db)
    _, attempt = await claim(db, A)
    await claim(db, A)  # the agent holds two tasks
    async with db.write() as tx:
        await accounts.lease(
            tx, A, ["anthropic"], store, task_id=first, attempt=attempt
        )
    await release(db, first)
    assert await active_leases(db, A) == ["engine"]


# -- operator stops of a whole agent or instance (3.4, 3.5) ----------------------------


async def test_a_draining_agent_that_leaves_releases_its_task(db: Database) -> None:
    await insert_agent(db, A)
    task_id = await new_task(db, max_attempts=1)
    await claim(db, A)
    async with db.write() as tx:
        await agents.drain(tx, A, "admin:ops")
        await agents.leave(tx, A)
    async with db.read() as conn:
        task = await tasks.get_task(conn, task_id)
    assert (task.state, task.failures) == (S.READY, 0)
    [event] = await events(db, task_id, "task.released")
    assert (event["reason"], event["actor"]) == ("drain", f"agent:{A}")


async def test_an_active_agent_that_leaves_still_counts_a_failure(
    db: Database,
) -> None:
    await insert_agent(db, A)
    task_id = await new_task(db)
    await claim(db, A)
    async with db.write() as tx:
        await agents.leave(tx, A)
    async with db.read() as conn:
        task = await tasks.get_task(conn, task_id)
    assert (task.state, task.failures) == (S.READY, 1)
    assert await events(db, task_id, "task.released") == []


async def test_a_silent_agent_and_a_timeout_still_count_failures(
    db: Database, clock: FakeClock
) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)
    lost = await new_task(db, priority=1)
    await claim(db, A)
    async with db.write() as tx:
        await agents.drain(tx, A, "admin:ops")  # draining, then it crashes
    timed = await new_task(db, timeout_s=60)
    async with db.write() as tx:
        await tasks.claim(tx, B, [])
    clock.advance(61)
    async with db.write() as tx:
        await agents.heartbeat(tx, B, [(timed, 1)])
        await tasks.expire_timeouts(tx)
    clock.advance(agents.LOST_AFTER_S + 1)
    async with db.write() as tx:
        assert await agents.mark_lost(tx) == [A, B]
    async with db.read() as conn:
        assert (await tasks.get_task(conn, lost)).failures == 1
        assert (await tasks.get_task(conn, timed)).failures == 1


SETTINGS = FleetSettings(argus_url="http://argus.test:8000", max_starts_in_flight=64)


@pytest.fixture
def target() -> FakeTarget:
    return FakeTarget()


@pytest.fixture
def registry(target: FakeTarget) -> TargetRegistry:
    r = TargetRegistry()
    r.override("docker", None, target)
    return r


async def busy_instance(db: Database, registry: TargetRegistry, target: FakeTarget):
    """A running instance whose agent holds a task on its last attempt."""
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(image="w:1", desired_replicas=1, max_replicas=2),
            "admin:test",
        )
    await reconcile_pool(db, "web", registry, SETTINGS)
    await reconcile_pool(db, "web", registry, SETTINGS)
    async with db.read() as conn:
        [inst] = await fleet.list_instances(conn, "web")
    token = target.specs[inst.provider_ref].env["ARGUS_BOOTSTRAP_TOKENS"]
    async with db.write() as tx:
        reg = await agents.register(tx, AgentRegister(bootstrap_token=token))
    task_id = await new_task(db, max_attempts=1)
    await claim(db, reg.agent.id)
    return inst, reg.agent.id, task_id


async def task_and_instance(db: Database, task_id: str, instance_id: str):
    async with db.read() as conn:
        return (
            await tasks.get_task(conn, task_id),
            await fleet.get_instance(conn, instance_id),
        )


async def test_recycle_with_requeue_releases_now_and_stops(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    inst, agent_id, task_id = await busy_instance(db, registry, target)
    async with db.write() as tx:
        assert await fleet.recycle(tx, inst.id, "admin:ops", requeue=True) == [task_id]
    task, instance = await task_and_instance(db, task_id, inst.id)
    assert (task.state, task.failures) == (S.READY, 0)
    assert instance.draining and instance.agents[0].state == "draining"
    [event] = await events(db, task_id, "task.released")
    assert (event["reason"], event["actor"], event["previous_holder"]) == (
        "recycle",
        "admin:ops",
        agent_id,
    )
    await reconcile_pool(db, "web", registry, SETTINGS)  # idle now: it is stopped
    _, instance = await task_and_instance(db, task_id, inst.id)
    assert instance.state == "terminating"


async def test_recycle_with_requeue_of_a_draining_instance_still_releases(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    inst, _, task_id = await busy_instance(db, registry, target)
    async with db.write() as tx:
        assert await fleet.recycle(tx, inst.id, "admin:ops") == []  # waits for work
    task, _ = await task_and_instance(db, task_id, inst.id)
    assert task.state is S.CLAIMED
    async with db.write() as tx:
        assert await fleet.recycle(tx, inst.id, "admin:ops", requeue=True) == [task_id]


async def test_a_recycled_instance_that_ends_releases_its_tasks(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    inst, _, task_id = await busy_instance(db, registry, target)
    async with db.write() as tx:
        await fleet.recycle(tx, inst.id, "admin:ops")
    target.exit(inst.provider_ref)  # e.g. its host was stopped mid-recycle
    await reconcile_pool(db, "web", registry, SETTINGS)
    task, instance = await task_and_instance(db, task_id, inst.id)
    assert instance.state == "terminated"
    assert (task.state, task.failures) == (S.READY, 0)
    [event] = await events(db, task_id, "task.released")
    assert event["reason"] == "recycle"


async def test_a_crashed_instance_still_counts_a_failure(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    inst, _, task_id = await busy_instance(db, registry, target)
    target.exit(inst.provider_ref)
    await reconcile_pool(db, "web", registry, SETTINGS)
    task, _ = await task_and_instance(db, task_id, inst.id)
    assert (task.state, task.failures) == (S.FAILED, 1)  # its only attempt


async def test_terminate_requeues_by_default(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    inst, _, task_id = await busy_instance(db, registry, target)
    async with db.write() as tx:
        await fleet.request_termination(tx, inst.id, "admin:ops")
    task, instance = await task_and_instance(db, task_id, inst.id)
    assert instance.state == "terminating"
    assert (task.state, task.failures) == (S.READY, 0)
    [event] = await events(db, task_id, "task.released")
    assert (event["reason"], event["actor"]) == ("operator_terminate", "admin:ops")


async def test_terminate_without_requeue_counts_a_failure(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    inst, _, task_id = await busy_instance(db, registry, target)
    async with db.write() as tx:
        await fleet.request_termination(tx, inst.id, "admin:ops", requeue=False)
    task, _ = await task_and_instance(db, task_id, inst.id)
    assert (task.state, task.failures) == (S.FAILED, 1)


# -- HTTP ------------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "r" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {res.json()['token']}"}


def claimed(client: TestClient, worker: dict[str, str]) -> tuple[str, int]:
    task = client.post("/api/tasks", json={"project": "global", "title": "x"}).json()
    got = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert got["id"] == task["id"]
    return task["id"], got["attempt"]


def test_release_over_http(client: TestClient) -> None:
    worker = register(client)
    orchestrator = register(client, "orchestrator")
    task_id, attempt = claimed(client, worker)
    url = f"/api/tasks/{task_id}/release"

    res = client.post(url, json={"reason": "recycle"}, headers=worker)
    assert res.status_code == 403
    for body in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}):
        assert client.post(url, json=body).status_code == 422
    res = client.post(url, json={"reason": "recycle", "attempt": attempt + 1})
    assert (res.status_code, res.json()["detail"]["code"]) == (409, "stale_attempt")

    res = client.post(
        url, json={"reason": "recycle", "attempt": attempt}, headers=orchestrator
    )
    assert res.status_code == 200
    assert (res.json()["state"], res.json()["failures"]) == ("ready", 0)
    res = client.post(url, json={"reason": "again"})
    assert (res.status_code, res.json()["detail"]["code"]) == (409, "task_not_claimed")

    # The old holder: heartbeat rejects the pair, FAIL is refused and counts nothing.
    beat = client.post(
        "/api/agents/me/heartbeat",
        json={"leases": [{"task_id": task_id, "attempt": attempt}]},
        headers=worker,
    ).json()
    assert [(r["task_id"], r["code"]) for r in beat["rejected"]] == [
        (task_id, "not_claimed")
    ]
    res = client.post(
        f"/api/tasks/{task_id}/fail",
        json={"attempt": attempt, "error": "interrupted"},
        headers=worker,
    )
    assert (res.status_code, res.json()["detail"]["code"]) == (409, "not_claimed")
    assert client.get(f"/api/tasks/{task_id}").json()["failures"] == 0
    [row] = client.get(f"/api/tasks/{task_id}/attempts").json()
    assert (row["outcome"], row["outcome_reason"]) == ("released", "recycle")

    # The worker claims it again at the next attempt; admins release too.
    retaken = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert (retaken["id"], retaken["attempt"]) == (task_id, attempt + 1)
    res = client.post(url, json={"reason": "operator"})
    assert res.status_code == 200


def test_a_release_wakes_a_waiting_claim(client: TestClient) -> None:
    holder, waiter = register(client), register(client)
    task_id, _ = claimed(client, holder)
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(
            client.post, "/api/tasks/claim", json={"wait_s": 10}, headers=waiter
        )
        time.sleep(0.3)
        started = time.monotonic()
        assert (
            client.post(
                f"/api/tasks/{task_id}/release", json={"reason": "recycle"}
            ).status_code
            == 200
        )
        res = waiting.result(timeout=5)
    assert (res.status_code, res.json()["id"]) == (200, task_id)
    assert time.monotonic() - started < 5


def test_terminate_and_recycle_take_requeue(client: TestClient) -> None:
    # Unknown instance: the body is accepted and the lookup answers 404.
    for path, body in (
        ("terminate", {"requeue": False}),
        ("terminate", None),
        ("recycle", {"requeue": True}),
        ("recycle", None),
    ):
        res = client.post(f"/api/instances/nope/{path}", json=body)
        assert res.status_code == 404, (path, body, res.text)
