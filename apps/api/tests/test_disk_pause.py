"""Disk guard pause foundations (operations.spec.md 3.5): dormant, guard-only state."""

import asyncio
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from api_support import DONE, FakeClock, insert_agent
from argus_api import reaper
from argus_api.agents import service as agents
from argus_api.db import Database, ts
from argus_api.inbox import inbox
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import service as tasks
from argus_api.tasks.views import read_task
from argus_core.tasks import LeaseObservation, TaskCreate
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient
from pydantic import ValidationError

A = "agent-a"
QUIET_S = 1200
STALL_S = 3600
SILENT = LeaseObservation(workspace="ok", session="running", delegate="silent")


@pytest.fixture(autouse=True)
async def agent_row(db: Database) -> None:
    await insert_agent(db, A)


async def claim(db: Database, **kw):
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t", **kw), "admin:test"
        )
        task = await tasks.claim(tx, A, [])
    assert task is not None
    return task


async def mark(db: Database, *claims) -> list[str]:
    async with db.write() as tx:
        return await tasks.mark_paused_disk(tx, [(t.id, t.attempt) for t in claims])


async def clear(db: Database, *claims) -> list[str]:
    async with db.write() as tx:
        return await tasks.clear_paused_disk(
            tx, [(t.id, t.attempt) for t in claims] if claims else None
        )


async def row(db: Database, task_id: str):
    async with (
        db.read() as conn,
        conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur,
    ):
        return await cur.fetchone()


async def reap(db: Database) -> reaper.ReapResult:
    async with db.write() as tx:
        return await reaper.reap(tx, quiet_claim_s=QUIET_S, stall_fail_s=STALL_S)


async def hold(db: Database, clock: FakeClock, seconds: int, *claims) -> None:
    """Let time pass: the agent heartbeats (no activity) the `claims` it lists, and
    the guard renews paused leases, the way it will once it pauses."""
    left = seconds
    while left > 0:
        step = min(left, 60)
        clock.advance(step)
        async with db.write() as tx:
            await agents.heartbeat(tx, A, [(t.id, t.attempt) for t in claims])
            await tasks.renew_paused_disk(tx)
        left -= step


# -- quiet, stalled, lease, timeout ------------------------------------------------


async def test_a_paused_claim_is_never_quiet_or_stalled(
    db: Database, clock: FakeClock
) -> None:
    paused, running = await claim(db), await claim(db)
    assert await mark(db, paused) == [paused.id]
    await hold(db, clock, QUIET_S + 60, running)  # only `running` heartbeats
    result = await reap(db)
    assert result.quieted_tasks == [running.id]
    await hold(db, clock, STALL_S, running)
    result = await reap(db)
    assert result.expired_tasks == [running.id]
    after = await row(db, paused.id)
    assert after["state"] == S.CLAIMED and after["quiet_at"] is None
    assert after["paused_disk_at"] is not None
    assert (await row(db, running.id))["state"] == S.READY


async def test_a_quiet_claim_paused_later_is_not_expired_as_stalled(
    db: Database, clock: FakeClock
) -> None:
    task = await claim(db)
    await hold(db, clock, QUIET_S + 60, task)
    assert (await reap(db)).quieted_tasks == [task.id]
    await mark(db, task)
    await hold(db, clock, STALL_S, task)
    assert (await reap(db)).expired_tasks == []
    assert (await row(db, task.id))["state"] == S.CLAIMED


async def test_the_guard_renews_only_paused_live_leases(
    db: Database, clock: FakeClock
) -> None:
    paused, other = await claim(db), await claim(db)
    await mark(db, paused)
    clock.advance(60)
    async with db.write() as tx:
        renewed = await tasks.renew_paused_disk(tx)
    assert [(r.task_id, r.attempt, r.reason) for r in renewed] == [
        (paused.id, paused.attempt, "paused_disk")
    ]
    assert renewed[0].lease_expires_at == clock.now + timedelta(
        seconds=paused.lease_ttl_s
    )
    assert datetime.fromisoformat((await row(db, paused.id))["lease_expires_at"]) == (
        renewed[0].lease_expires_at
    )
    assert (await row(db, other.id))["lease_expires_at"] == ts(other.lease_expires_at)

    # Over three TTLs with no worker heartbeat: the guard alone keeps the lease.
    await hold(db, clock, 3 * paused.lease_ttl_s)
    result = await reap(db)
    assert other.id in result.expired_tasks and paused.id not in result.expired_tasks


async def test_an_expired_paused_lease_is_not_revived(
    db: Database, clock: FakeClock
) -> None:
    task = await claim(db)
    await mark(db, task)
    clock.advance(task.lease_ttl_s + 1)
    async with db.write() as tx:
        assert await tasks.renew_paused_disk(tx) == []
    assert (await reap(db)).expired_tasks == [task.id]
    after = await row(db, task.id)
    assert (after["state"], after["paused_disk_at"]) == (S.READY, None)


async def test_a_paused_claim_still_times_out(db: Database, clock: FakeClock) -> None:
    task = await claim(db, timeout_s=300, max_attempts=1)
    await mark(db, task)
    await hold(db, clock, 301)
    assert (await reap(db)).expired_tasks == [task.id]
    after = await row(db, task.id)
    assert (after["state"], after["paused_disk_at"]) == (S.FAILED, None)
    assert "stall cause: paused disk" in after["error"]


# -- resume -------------------------------------------------------------------------


async def test_resume_restarts_the_quiet_clock(db: Database, clock: FakeClock) -> None:
    task = await claim(db)
    await hold(db, clock, QUIET_S + 60, task)
    assert (await reap(db)).quieted_tasks == [task.id]
    await mark(db, task)
    await hold(db, clock, STALL_S, task)
    assert await clear(db, task) == [task.id]
    after = await row(db, task.id)
    assert after["quiet_at"] is None and after["paused_disk_at"] is None
    assert after["activity_at"] == ts(clock.now)
    await hold(db, clock, QUIET_S - 60, task)
    assert (await reap(db)).quieted_tasks == []
    await hold(db, clock, 120, task)
    assert (await reap(db)).quieted_tasks == [task.id]


async def test_resume_and_expiry_in_one_transaction_never_expire_the_resumed(
    db: Database, clock: FakeClock
) -> None:
    task = await claim(db)
    await hold(db, clock, QUIET_S + 60, task)
    await reap(db)
    await mark(db, task)
    await hold(db, clock, STALL_S, task)
    async with db.write() as tx:
        assert await tasks.clear_paused_disk(tx, [(task.id, task.attempt)])
        assert await tasks.expire_stalled(tx, STALL_S) == []
        assert await tasks.flag_quiet(tx, QUIET_S) == []
        assert await tasks.expire_timeouts(tx) == []
        assert await tasks.expire_leases(tx) == []
    assert (await row(db, task.id))["state"] == S.CLAIMED


async def test_expiry_then_resume_is_a_no_op_on_the_next_attempt(
    db: Database, clock: FakeClock
) -> None:
    task = await claim(db, timeout_s=300, max_attempts=3)
    await mark(db, task)
    await hold(db, clock, 301)
    async with db.write() as tx:
        assert await tasks.expire_timeouts(tx) == [task.id]
        assert await tasks.clear_paused_disk(tx, [(task.id, task.attempt)]) == []
    again = await claim(db)
    assert (again.id, again.attempt) == (task.id, task.attempt + 1)
    assert (await row(db, task.id))["paused_disk_at"] is None
    assert await mark(db, task) == []  # the old attempt
    assert await clear(db) == []


async def test_concurrent_mark_or_resume_and_reaper_are_serialized(
    db: Database, clock: FakeClock
) -> None:
    """Each pairing lands as one whole outcome, never half of each."""
    task = await claim(db, max_attempts=3)
    await hold(db, clock, QUIET_S + 60, task)
    await reap(db)
    await hold(db, clock, STALL_S, task)  # stalled: the reaper would expire it

    marked, result = await asyncio.gather(mark(db, task), reap(db))
    after = await row(db, task.id)
    if result.expired_tasks:  # the reaper won: the pause found no claim
        assert marked == [] and after["state"] == S.READY
        assert after["paused_disk_at"] is None
        return
    assert marked == [task.id] and after["state"] == S.CLAIMED
    cleared, result = await asyncio.gather(clear(db, task), reap(db))
    after = await row(db, task.id)
    assert cleared == [task.id] and result.expired_tasks == []
    assert (after["state"], after["paused_disk_at"]) == (S.CLAIMED, None)


async def test_the_reaper_running_first_expires_before_the_pause_lands(
    db: Database, clock: FakeClock
) -> None:
    task = await claim(db, max_attempts=3)
    await hold(db, clock, QUIET_S + 60, task)
    await reap(db)
    await hold(db, clock, STALL_S, task)
    assert (await reap(db)).expired_tasks == [task.id]
    assert await mark(db, task) == []
    assert (await row(db, task.id))["paused_disk_at"] is None


# -- who may set it -----------------------------------------------------------------


async def test_mark_skips_unclaimed_and_wrong_attempts(db: Database) -> None:
    task = await claim(db)
    async with db.write() as tx:
        ready = await tasks.create_task(
            tx, TaskCreate(project="global", title="r"), "admin:test"
        )
    async with db.write() as tx:
        assert await tasks.mark_paused_disk(tx, [(ready.id, 0)]) == []
        assert await tasks.mark_paused_disk(tx, [(task.id, task.attempt + 1)]) == []
        assert await tasks.mark_paused_disk(tx, [("missing", 1)]) == []
        assert await tasks.mark_paused_disk(tx, [(task.id, task.attempt)]) == [task.id]
        assert await tasks.mark_paused_disk(tx, [(task.id, task.attempt)]) == []
        assert await tasks.clear_paused_disk(tx, [(task.id, task.attempt + 1)]) == []
    assert (await row(db, ready.id))["paused_disk_at"] is None
    assert (await row(db, task.id))["paused_disk_at"] is not None


async def test_worker_heartbeats_never_set_or_clear_the_pause(
    db: Database, clock: FakeClock
) -> None:
    paused, running = await claim(db), await claim(db)
    await mark(db, paused)
    before = (await row(db, paused.id))["paused_disk_at"]
    for obs in (
        None,
        SILENT,
        LeaseObservation(workspace="missing", session="gone", delegate="active"),
        LeaseObservation(),
    ):
        clock.advance(10)
        async with db.write() as tx:
            await agents.heartbeat(
                tx,
                A,
                [
                    (paused.id, paused.attempt, clock.now, obs),
                    (running.id, running.attempt, clock.now, obs),
                ],
            )
        assert (await row(db, paused.id))["paused_disk_at"] == before
        assert (await row(db, running.id))["paused_disk_at"] is None
    with pytest.raises(ValidationError):
        LeaseObservation.model_validate({"session": "paused_disk"})
    assert "paused_disk" not in LeaseObservation.model_fields


# -- every claim exit clears it -------------------------------------------------------


@pytest.mark.parametrize("exit_", ["complete", "fail", "release", "expire"])
async def test_every_claim_exit_clears_the_pause(
    db: Database, clock: FakeClock, exit_: str
) -> None:
    task = await claim(db, max_attempts=3)
    await mark(db, task)
    async with db.write() as tx:
        match exit_:
            case "complete":
                await tasks.complete(tx, A, task.id, task.attempt, DONE)
            case "fail":
                await tasks.fail(tx, A, task.id, task.attempt, "boom")
            case "release":
                await tasks.release(tx, task.id, "admin:test", "test")
            case "expire":
                await tasks.expire_agent_leases(tx, A)
    after = await row(db, task.id)
    assert after["state"] != S.CLAIMED and after["paused_disk_at"] is None


# -- visibility -----------------------------------------------------------------------


async def test_stall_cause_is_paused_disk_in_read_and_inbox(
    db: Database, clock: FakeClock
) -> None:
    task = await claim(db)
    async with db.write() as tx:  # whatever the host saw, the API's flag wins
        await agents.heartbeat(tx, A, [(task.id, task.attempt, None, SILENT)])
    await hold(db, clock, QUIET_S + 60, task)
    await reap(db)
    await mark(db, task)
    async with db.read() as conn:
        read = await read_task(conn, task.id, clock.now)
        facts = await tasks.quiet_facts_for(conn, [task.id], clock.now)
    async with db.snapshot() as snap:
        page = await inbox(snap)
    assert read.stall_cause == "paused_disk"
    assert facts[task.id].stall_cause == "paused_disk"
    item = next(i for i in page.items if i.subject_id == task.id)
    assert item.stall_cause == "paused_disk"

    await clear(db, task)
    async with db.read() as conn:
        assert (await read_task(conn, task.id, clock.now)).stall_cause is None


async def test_a_paused_claim_not_yet_quiet_reads_paused_disk(
    db: Database, clock: FakeClock
) -> None:
    task = await claim(db)
    await mark(db, task)
    async with db.read() as conn:
        read = await read_task(conn, task.id, clock.now)
    assert (read.quiet_at, read.stall_cause) == (None, "paused_disk")


# -- HTTP ----------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "x" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers=ADMIN) as c:
        app.state.secrets = MemorySecretStore()
        yield c


def test_http_heartbeat_cannot_claim_a_pause_and_reads_show_it(
    client: TestClient,
) -> None:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    worker = {"Authorization": f"Bearer {res.json()['token']}"}
    client.post("/api/tasks", json={"project": "global", "title": "p"})
    task = client.post("/api/tasks/claim", json={}, headers=worker).json()
    lease = {"task_id": task["id"], "attempt": task["attempt"]}

    res = client.post(
        "/api/agents/me/heartbeat",
        json={"leases": [lease | {"observation": {"session": "paused_disk"}}]},
        headers=worker,
    )
    assert res.status_code == 422
    res = client.post(
        "/api/agents/me/heartbeat",
        json={"leases": [lease | {"paused_disk": True, "reason": "paused_disk"}]},
        headers=worker,
    )
    assert res.status_code == 200, res.text
    assert client.get(f"/api/tasks/{task['id']}").json()["stall_cause"] is None

    async def pause(db) -> None:
        async with db.write() as tx:
            await tasks.mark_paused_disk(tx, [(task["id"], task["attempt"])])

    client.portal.call(pause, app.state.db)  # type: ignore[union-attr]
    assert client.get(f"/api/tasks/{task['id']}").json()["stall_cause"] == (
        "paused_disk"
    )
