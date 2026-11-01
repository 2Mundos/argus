"""OOM evidence and the `env_oom` promotion (failure-classes.spec.md 3.2.1)."""

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api import fleet_health
from argus_api.db import Database, ts
from argus_api.errors import LeaseConflictError, ValidationFailedError
from argus_api.fleet import service as fleet
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.fleet import PoolUpsert
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

MARKER = "SYNTHETIC-OOM-MARKER-7f2b"
ERROR = f"session container exited (code 137); last output: {MARKER}"


# -- helpers -----------------------------------------------------------------------


async def pool(db: Database, name: str, memory_mb: int | None = 512) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            name,
            PoolUpsert(
                runtime="plain", image="img", max_replicas=1, memory_mb=memory_mb
            ),
            "human:test",
        )


async def agent(db: Database, pool_name: str | None, agent_id: str) -> str:
    """An agent on an instance of `pool_name` (None: outside any pool)."""
    async with db.write() as tx:
        if pool_name is not None:
            await tx.conn.execute(
                "INSERT INTO instances (id, pool, provider_ref, state, created_at)"
                " VALUES (?, ?, 'ref', 'running', ?)",
                (f"i-{agent_id}", pool_name, ts(tx.now)),
            )
        await tx.conn.execute(
            "INSERT INTO agents (id, name, role, instance_id, capabilities, state,"
            " last_heartbeat_at, created_at) VALUES (?, ?, 'worker', ?, '[]',"
            " 'active', ?, ?)",
            (
                agent_id,
                agent_id,
                None if pool_name is None else f"i-{agent_id}",
                ts(tx.now),
                ts(tx.now),
            ),
        )
    return agent_id


async def new_task(db: Database, title: str = "big", **kw) -> str:
    kw.setdefault("max_attempts", 10)
    kw.setdefault("lease_ttl_s", 3600)
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="demo", title=title, **kw), "human:test"
        )
    return task.id


async def claim(db: Database, agent_id: str, task_id: str) -> int:
    async with db.write() as tx:
        task = await tasks.claim(tx, agent_id, [])
    assert task is not None and task.id == task_id
    return task.attempt


async def lose(
    db: Database, agent_id: str, task_id: str, attempt: int, *, oom: bool = True
):
    async with db.write() as tx:
        return await tasks.fail(
            tx, agent_id, task_id, attempt, ERROR, "session_lost", oom_kill=oom
        )


async def run_oom(db: Database, clock: FakeClock, agent_id: str, task_id: str):
    """Claim the task and lose its session to an OOM kill."""
    attempt = await claim(db, agent_id, task_id)
    clock.advance(30)
    return await lose(db, agent_id, task_id, attempt)


async def complete_one(db: Database, agent_id: str) -> None:
    task_id = await new_task(db, "small", priority=100)
    attempt = await claim(db, agent_id, task_id)
    async with db.write() as tx:
        await tasks.complete(
            tx, agent_id, task_id, attempt, tasks.TaskResult(summary="ok")
        )


async def streak(db: Database, name: str) -> int:
    async with (
        db.read() as conn,
        conn.execute("SELECT oom_streak FROM pools WHERE name = ?", (name,)) as cur,
    ):
        return (await cur.fetchone())[0]


async def events(db: Database, task_id: str, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        ) as cur,
    ):
        return [json.loads(r["data"]) for r in await cur.fetchall()]


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def unblock(db: Database, task_id: str, actor: str = "human:op") -> None:
    async with db.write() as tx:
        await tasks.unblock(tx, task_id, actor)


@pytest.fixture
async def p1(db: Database) -> str:
    await pool(db, "p1")
    return await agent(db, "p1", "a1")


# -- first OOM: a session exit under 3.2 ---------------------------------------------


async def test_first_oom_is_a_session_exit_with_the_fact(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db)
    task = await run_oom(db, clock, p1, task_id)
    assert task.state == "ready" and task.failures == 1 and task.oom_kill
    assert task.block_reason is None
    (fail,) = await events(db, task_id, "task.fail")
    assert fail["reason"] == "session_lost" and fail["retry_class"] == "session"
    assert (fail["oom_kill"], fail["pool"], fail["oom_streak"]) == (True, "p1", 1)
    assert len(await events(db, task_id, "task.auto_resumed")) == 1
    assert await streak(db, "p1") == 1


@pytest.mark.parametrize("progressed", [False, True], ids=["no_progress", "progress"])
async def test_first_oom_keeps_the_progress_gate(
    db: Database, clock: FakeClock, p1: str, progressed: bool
) -> None:
    """After a resumed non-OOM exit, the first OOM is gated like any exit."""
    task_id = await new_task(db)
    attempt = await claim(db, p1, task_id)
    clock.advance(30)
    assert (await lose(db, p1, task_id, attempt, oom=False)).state == "ready"
    attempt = await claim(db, p1, task_id)
    if progressed:
        async with db.write() as tx:
            await tasks.report_progress(tx, p1, task_id, attempt, "working")
    clock.advance(30)
    task = await lose(db, p1, task_id, attempt)
    assert task.state == ("ready" if progressed else "failed")
    assert task.failures == 2 and task.oom_kill
    assert await streak(db, "p1") == 1
    last = (await events(db, task_id, "task.fail"))[-1]
    assert last["oom_kill"] is True
    assert last.get("auto_resume") == (None if progressed else "no_progress")


# -- the promotion -----------------------------------------------------------------


async def test_second_consecutive_oom_on_the_pool_blocks_env_oom(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db, max_attempts=5)
    await run_oom(db, clock, p1, task_id)
    attempt = await claim(db, p1, task_id)
    async with db.write() as tx:
        await tasks.report_progress(
            tx,
            p1,
            task_id,
            attempt,
            "pushed",
            kind="commit_created",
            data={"branch": "wip/oom", "sha": "abc1234"},
        )
    clock.advance(30)
    task = await lose(db, p1, task_id, attempt)
    assert (task.state, task.block_reason) == ("blocked", "env_oom")
    assert (task.failures, task.max_attempts, task.attempt) == (2, 5, 2)
    assert task.assignee_agent_id is None and task.lease_expires_at is None
    assert task.oom_kill and MARKER in (task.error or "")
    assert "abc1234" in {a.ref for a in task.continuation.artifacts}
    (block,) = await events(db, task_id, "task.block")
    assert block == {
        "from": "claimed",
        "to": "blocked",
        "attempt": 2,
        "error": ERROR,
        "reason": "env_oom",
        "cause": "session_lost",
        "retry_class": "environment",
        "oom_kill": True,
        "pool": "p1",
        "oom_streak": 2,
    }
    assert len(await events(db, task_id, "task.auto_resumed")) == 1
    assert len(await events(db, task_id, "task.fail")) == 1
    assert await streak(db, "p1") == 2


async def test_unrelated_tasks_on_one_pool_share_the_streak(
    db: Database, clock: FakeClock, p1: str
) -> None:
    other = await agent(db, "p1", "a2")
    first = await new_task(db, "one")
    assert (await run_oom(db, clock, p1, first)).state == "ready"
    second = await new_task(db, "two", priority=100)
    task = await run_oom(db, clock, other, second)
    assert (task.state, task.block_reason, task.failures) == ("blocked", "env_oom", 1)
    assert (await get(db, first)).state == "ready"


@pytest.mark.parametrize("between", ["complete", "session_lost", "worker_fail"])
async def test_a_non_oom_outcome_between_resets_the_streak(
    db: Database, clock: FakeClock, p1: str, between: str
) -> None:
    task_id = await new_task(db)
    await run_oom(db, clock, p1, task_id)
    if between == "complete":
        await complete_one(db, p1)
    else:
        other = await new_task(db, "other", priority=100, max_attempts=1)
        attempt = await claim(db, p1, other)
        async with db.write() as tx:
            await tasks.fail(tx, p1, other, attempt, "no", between)
    assert await streak(db, "p1") == 0
    task = await run_oom(db, clock, p1, task_id)
    # Not promoted: the progress gate (no progress since the resume) ends it.
    assert task.state == "failed" and task.block_reason is None
    assert (await events(db, task_id, "task.fail"))[-1]["auto_resume"] == "no_progress"
    assert await streak(db, "p1") == 1


async def test_admission_fail_leaves_the_streak(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db)
    await run_oom(db, clock, p1, task_id)
    attempt = await claim(db, p1, task_id)
    async with db.write() as tx:
        await tasks.fail(tx, p1, task_id, attempt, "limit", "quota")
    assert await streak(db, "p1") == 1


async def test_pools_never_mix(db: Database, clock: FakeClock, p1: str) -> None:
    await pool(db, "p2")
    a2 = await agent(db, "p2", "b1")
    one = await new_task(db, "one")
    await run_oom(db, clock, p1, one)
    two = await new_task(db, "two", priority=100)
    async with db.write() as tx:
        held = await tasks.claim(tx, a2, [])
    assert held is not None and held.id == two
    clock.advance(30)
    assert (await lose(db, a2, two, held.attempt)).state == "ready"
    assert (await streak(db, "p1"), await streak(db, "p2")) == (1, 1)


async def test_an_agent_outside_any_pool_records_the_fact_and_never_blocks(
    db: Database, clock: FakeClock
) -> None:
    loose = await agent(db, None, "loose")
    task_id = await new_task(db)
    await run_oom(db, clock, loose, task_id)
    attempt = await claim(db, loose, task_id)
    async with db.write() as tx:
        await tasks.report_progress(tx, loose, task_id, attempt, "working")
    clock.advance(30)
    task = await lose(db, loose, task_id, attempt)
    assert task.state == "ready" and task.oom_kill
    fails = await events(db, task_id, "task.fail")
    assert [f["oom_kill"] for f in fails] == [True, True]
    assert all("pool" not in f for f in fails)


async def test_the_cap_wins_over_the_block(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db, max_attempts=2)
    await run_oom(db, clock, p1, task_id)
    task = await run_oom(db, clock, p1, task_id)
    assert (task.state, task.failures) == ("failed", 2)
    assert task.block_reason is None and task.oom_kill
    last = (await events(db, task_id, "task.fail"))[-1]
    assert (last["oom_kill"], last["oom_streak"]) == (True, 2)


# -- what records nothing ----------------------------------------------------------


async def test_a_stale_or_repeated_fail_records_nothing(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db)
    attempt = await claim(db, p1, task_id)
    clock.advance(30)
    await lose(db, p1, task_id, attempt)
    for repeat in (attempt, attempt + 5):
        with pytest.raises(LeaseConflictError):
            await lose(db, p1, task_id, repeat)
    intruder = await agent(db, "p1", "a9")
    with pytest.raises(LeaseConflictError):
        await lose(db, intruder, task_id, attempt)
    assert await streak(db, "p1") == 1
    assert len(await events(db, task_id, "task.fail")) == 1


async def test_oom_kill_needs_session_lost(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db)
    attempt = await claim(db, p1, task_id)
    async with db.write() as tx:
        with pytest.raises(ValidationFailedError):
            await tasks.fail(
                tx, p1, task_id, attempt, "x", "worker_fail", oom_kill=True
            )
    assert await streak(db, "p1") == 0
    assert (await get(db, task_id)).state == "claimed"


async def test_the_reaper_never_promotes_and_resets(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db, lease_ttl_s=60)
    await run_oom(db, clock, p1, task_id)
    await claim(db, p1, task_id)
    clock.advance(120)
    async with db.write() as tx:
        assert await tasks.expire_leases(tx) == [task_id]
    task = await get(db, task_id)
    assert task.state == "ready" and task.block_reason is None and not task.oom_kill
    assert await streak(db, "p1") == 0
    (expire,) = await events(db, task_id, "task.expire")
    assert "oom_kill" not in expire


# -- resets: unblock, memory, retry ------------------------------------------------


async def promoted(db: Database, clock: FakeClock, agent_id: str, **kw) -> str:
    task_id = await new_task(db, **kw)
    await run_oom(db, clock, agent_id, task_id)
    task = await run_oom(db, clock, agent_id, task_id)
    assert task.block_reason == "env_oom"
    return task_id


async def test_unblock_resets_the_pool_and_starts_a_new_epoch(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await promoted(db, clock, p1)
    await unblock(db, task_id)
    task = await get(db, task_id)
    assert (task.state, task.failures, task.block_reason) == ("ready", 2, None)
    assert await streak(db, "p1") == 0
    # A new epoch: one automatic resume again, then the next OOM blocks again.
    assert (await run_oom(db, clock, p1, task_id)).state == "ready"
    assert (await run_oom(db, clock, p1, task_id)).block_reason == "env_oom"


async def test_a_system_unblock_does_not_reset(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await promoted(db, clock, p1)
    await unblock(db, task_id, "system")
    assert await streak(db, "p1") == 2


async def test_the_loop_ends_at_max_attempts(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await promoted(db, clock, p1, max_attempts=5)
    await unblock(db, task_id)
    await run_oom(db, clock, p1, task_id)
    assert (await run_oom(db, clock, p1, task_id)).block_reason == "env_oom"
    await unblock(db, task_id)
    task = await run_oom(db, clock, p1, task_id)
    assert (task.state, task.failures) == ("failed", 5)


@pytest.mark.parametrize(("memory_mb", "kept"), [(1024, 0), (512, 2), (None, 0)])
async def test_a_memory_change_resets_the_streak(
    db: Database, clock: FakeClock, p1: str, memory_mb: int | None, kept: int
) -> None:
    await promoted(db, clock, p1)
    await pool(db, "p1", memory_mb)
    assert await streak(db, "p1") == kept


async def test_retry_clears_the_fact_but_not_the_streak(
    db: Database, clock: FakeClock, p1: str
) -> None:
    task_id = await new_task(db, max_attempts=2)
    await run_oom(db, clock, p1, task_id)
    assert (await run_oom(db, clock, p1, task_id)).state == "failed"
    async with db.write() as tx:
        task = await tasks.retry(tx, task_id, "human:op")
    assert task.state == "ready" and not task.oom_kill and task.failures == 0
    assert await streak(db, "p1") == 2
    assert (await run_oom(db, clock, p1, task_id)).block_reason == "env_oom"


# -- visibility --------------------------------------------------------------------


async def test_fleet_health_shows_the_streak_and_the_block(
    db: Database, clock: FakeClock, p1: str
) -> None:
    await promoted(db, clock, p1)
    health = await fleet_health.fleet_health(db, 24, now=clock.now)
    (row,) = [p for p in health.pools if p.name == "p1"]
    assert row.oom_streak == 2
    groups = {g.reason: g for g in health.failures.groups}
    assert groups["env_oom"].count == 1 and groups["session_lost"].count == 1


# -- over HTTP ---------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "o" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def test_the_fail_route_carries_the_fact(client: TestClient) -> None:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    h = {"Authorization": f"Bearer {reg.json()['token']}"}
    created = client.post(
        "/api/tasks", json={"title": "t", "project": "demo", "spec": "s"}
    )
    task_id = created.json()["id"]
    attempt = client.post("/api/tasks/claim", json={}, headers=h).json()["attempt"]
    url = f"/api/tasks/{task_id}/fail"
    body = {"attempt": attempt, "error": ERROR, "oom_kill": True}
    bad = client.post(url, json=body | {"reason": "worker_fail"}, headers=h)
    assert bad.status_code == 422
    ok = client.post(url, json=body | {"reason": "session_lost"}, headers=h)
    assert ok.status_code == 200 and ok.json()["oom_kill"] is True
    assert (
        client.post(url, json=body | {"reason": "session_lost"}, headers=h).status_code
        == 409
    )
