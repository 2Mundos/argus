"""Stall causes from the host's lease observation (stall-visibility.spec.md 3.7)."""

import asyncio
import json
from datetime import timedelta

import pytest
from api_support import FakeClock
from argus_api.agents import service as agents
from argus_api.agents.routes import LeaseRef
from argus_api.agents.service import LOST_AFTER_S
from argus_api.db import Database, ts
from argus_api.inbox import inbox
from argus_api.tasks import service as tasks
from argus_api.tasks.routes import BlockRequest
from argus_core.continuation import ContinuationUpdate
from argus_core.tasks import LeaseObservation, RetryClass, retry_class
from pydantic import ValidationError
from test_stall_visibility import claim_one, reap, register

MISSING = LeaseObservation(workspace="missing", session="running", delegate="silent")
GONE = LeaseObservation(workspace="ok", session="gone", delegate="silent")
BUSY = LeaseObservation(workspace="ok", session="running", delegate="active")
SILENT = LeaseObservation(workspace="ok", session="running", delegate="silent")
UNSURE = LeaseObservation(workspace="unknown", session="running", delegate="silent")


async def beat(db: Database, agent_id: str, task, obs=None, activity_at=None) -> None:
    async with db.write() as tx:
        await agents.heartbeat(
            tx, agent_id, [(task.id, task.attempt, activity_at, obs)]
        )


async def hold(db: Database, clock: FakeClock, agent_id: str, task, seconds, obs=None):
    """Advance time while heartbeating (no activity) with `obs` on every beat."""
    left = seconds
    while left > 0:
        step = min(left, LOST_AFTER_S - 1, 60)
        clock.advance(step)
        await beat(db, agent_id, task, obs)
        left -= step


async def row(db: Database, task_id: str):
    async with (
        db.read() as conn,
        conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur,
    ):
        return await cur.fetchone()


async def events(db: Database, task_id: str, kind: str) -> list[dict]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT data FROM events WHERE subject_id = ? AND kind = ? ORDER BY id",
            (task_id, kind),
        ) as cur,
    ):
        return [json.loads(r["data"]) for r in await cur.fetchall()]


async def keep_wip(db: Database, task_id: str) -> None:
    async with db.read() as conn:
        rev = (await tasks.get_task(conn, task_id)).continuation.revision
    async with db.write() as tx:
        await tasks.update_continuation(
            tx,
            task_id,
            ContinuationUpdate(
                expected_revision=rev,
                reason="test",
                wip={"summary": "local commit ab33ee0 not pushed", "ref": "ab33ee0"},
            ),
            "orchestrator:test",
        )


# -- derivation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("obs", "cause"),
    [
        (MISSING, "workspace_missing"),
        (GONE, "session_gone"),
        (BUSY, "silent_delegate_active"),
        (SILENT, "no_progress"),
        (UNSURE, "unknown"),
        (LeaseObservation(), "unknown"),
    ],
)
def test_cause_for_each_observation(obs: LeaseObservation, cause: str) -> None:
    now = FakeClock().now
    at = ts(now - timedelta(seconds=30))
    assert tasks.stall_cause(now, obs.model_dump_json(), at) == (cause, at)


def test_absent_stale_or_previous_attempt_observation_is_unknown() -> None:
    now = FakeClock().now
    obs = MISSING.model_dump_json()
    assert tasks.stall_cause(now, None, None) == ("unknown", None)
    edge = ts(now - timedelta(seconds=tasks.OBSERVATION_STALE_S))
    assert tasks.stall_cause(now, obs, edge)[0] == "workspace_missing"
    stale = ts(now - timedelta(seconds=tasks.OBSERVATION_STALE_S + 1))
    assert tasks.stall_cause(now, obs, stale) == ("unknown", stale)
    # Sent for the previous attempt, before this claim.
    claimed = ts(now - timedelta(seconds=5))
    old = ts(now - timedelta(seconds=10))
    assert tasks.stall_cause(now, obs, old, claimed)[0] == "unknown"


def test_lease_observation_is_a_closed_enum_and_extra_keys_are_ignored() -> None:
    with pytest.raises(ValidationError):
        LeaseRef.model_validate(
            {"task_id": "t", "attempt": 1, "observation": {"workspace": "gone"}}
        )
    ref = LeaseRef.model_validate(
        {
            "task_id": "t",
            "attempt": 1,
            "observation": {"workspace": "missing", "probe": "/srv/x", "future": 1},
        }
    )
    assert ref.observation == LeaseObservation(workspace="missing")


# -- storage --------------------------------------------------------------------------


async def test_heartbeat_stores_the_newest_observation(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    clock.advance(5)
    await beat(db, reg.agent.id, task, MISSING)
    first = await row(db, task.id)
    assert first["observation_at"] == ts(clock.now)
    assert LeaseObservation.model_validate_json(first["observation"]) == MISSING
    clock.advance(5)
    await beat(db, reg.agent.id, task)  # no observation: the last one stays
    assert (await row(db, task.id))["observation_at"] == first["observation_at"]
    await beat(db, reg.agent.id, task, SILENT)
    assert (await row(db, task.id))["observation_at"] == ts(clock.now)
    # A transaction with an older clock never moves it backwards.
    clock.advance(-60)
    await beat(db, reg.agent.id, task, GONE)
    after = await row(db, task.id)
    assert LeaseObservation.model_validate_json(after["observation"]) == SILENT


# -- quiet, expiry, inbox -----------------------------------------------------------


async def test_quiet_event_and_inbox_carry_the_cause(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    await hold(db, clock, reg.agent.id, task, 1300, MISSING)
    assert (await reap(db)).quieted_tasks == [task.id]
    [quiet] = await events(db, task.id, "task.quiet")
    assert quiet["stall_cause"] == "workspace_missing"
    assert quiet["observation_at"] == ts(clock.now)
    async with db.snapshot() as snap:
        item = next(i for i in (await inbox(snap)).items if i.subject_id == task.id)
    assert item.stall_cause == "workspace_missing"
    clock.advance(tasks.OBSERVATION_STALE_S + 1)  # the host stopped observing
    async with db.snapshot() as snap:
        item = next(i for i in (await inbox(snap)).items if i.subject_id == task.id)
    assert item.stall_cause == "unknown"


async def test_stalled_expiry_names_the_cause_and_keeps_the_wip(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    await keep_wip(db, task.id)
    await hold(db, clock, reg.agent.id, task, 1300, GONE)
    await reap(db, stall_fail_s=3600)
    await hold(db, clock, reg.agent.id, task, 2400, GONE)
    assert (await reap(db, stall_fail_s=3600)).expired_tasks == [task.id]
    [expire] = await events(db, task.id, "task.expire")
    assert (expire["reason"], expire["stall_cause"]) == ("stalled", "session_gone")
    assert expire["observation_at"] is not None
    assert "stall cause: session gone" in expire["error"]
    after = await row(db, task.id)
    assert (after["state"], after["failures"]) == ("ready", 1)
    assert after["observation"] is None and after["observation_at"] is None
    async with db.read() as conn:
        c = (await tasks.get_task(conn, task.id)).continuation
    assert c.wip is not None and c.wip.ref == "ab33ee0"  # requeue keeps the WIP
    assert c.interruption is not None and c.interruption.reason == "stalled"


async def test_lease_expiry_without_observation_reports_unknown(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    clock.advance(task.lease_ttl_s + 1)
    async with db.write() as tx:
        await agents.heartbeat(tx, reg.agent.id, [])
    assert (await reap(db)).expired_tasks == [task.id]
    [expire] = await events(db, task.id, "task.expire")
    assert (expire["stall_cause"], expire["observation_at"]) == ("unknown", None)
    assert "stall cause" not in expire["error"]


@pytest.mark.parametrize("obs", [None, MISSING, UNSURE], ids=["none", "miss", "unsure"])
@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "at", "above"])
async def test_thresholds_do_not_move_with_the_cause(
    db: Database, clock: FakeClock, obs, delta: int
) -> None:
    """Quiet at exactly 1200 s and stalled at exactly 3600 s of silence, whatever
    the observation says; nothing happens a second before."""
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    await hold(db, clock, reg.agent.id, task, 1200 + delta, obs)
    assert (await reap(db, stall_fail_s=3600)).quieted_tasks == (
        [task.id] if delta >= 0 else []
    )
    if delta < 0:
        await hold(db, clock, reg.agent.id, task, 1, obs)
        await reap(db, stall_fail_s=3600)
    await hold(db, clock, reg.agent.id, task, 3600 + delta - 1200 - max(delta, 0), obs)
    assert (await row(db, task.id))["state"] == "claimed"
    expired = (await reap(db, stall_fail_s=3600)).expired_tasks
    assert expired == ([task.id] if delta >= 0 else [])
    assert (await row(db, task.id))["failures"] == (1 if delta >= 0 else 0)


async def test_heartbeat_racing_the_reaper_never_expires_a_healthy_claim(
    db: Database, clock: FakeClock
) -> None:
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    await hold(db, clock, reg.agent.id, task, 1300, UNSURE)
    assert (await reap(db, stall_fail_s=3600)).quieted_tasks == [task.id]
    await hold(db, clock, reg.agent.id, task, 3599 - 1300, UNSURE)
    clock.advance(1)  # exactly at the stall threshold, as the fresh signal lands
    fresh = beat(db, reg.agent.id, task, BUSY, activity_at=clock.now)
    _, first = await asyncio.gather(fresh, reap(db, stall_fail_s=3600))
    assert first.expired_tasks == [] and first.quieted_tasks == []
    for i in range(20):  # healthy from here on, in either commit order
        clock.advance(50)
        fresh = beat(db, reg.agent.id, task, BUSY, activity_at=clock.now)
        passes = (fresh, reap(db, quiet_claim_s=60, stall_fail_s=120))
        results = await asyncio.gather(*(passes if i % 2 else passes[::-1]))
        result = results[1] if i % 2 else results[0]
        assert result.expired_tasks == [] and result.quieted_tasks == []
    after = await row(db, task.id)
    assert (after["state"], after["failures"]) == ("claimed", 0)
    assert after["quiet_at"] is None


# -- env_workspace_missing block ------------------------------------------------------


async def test_workspace_missing_block_spends_no_attempt_and_keeps_the_wip(
    db: Database, clock: FakeClock
) -> None:
    assert BlockRequest(attempt=1, reason="env_workspace_missing").reason
    assert retry_class("env_workspace_missing") is RetryClass.ENVIRONMENT
    reg = await register(db)
    task = await claim_one(db, reg.agent.id)
    await keep_wip(db, task.id)
    async with db.write() as tx:
        blocked = await tasks.block(
            tx,
            reg.agent.id,
            task.id,
            task.attempt,
            "env_workspace_missing",
            "agent group directory missing on the host for 90 s",
        )
    assert (blocked.state, blocked.failures, blocked.attempt) == (
        "blocked",
        0,
        task.attempt,
    )
    assert blocked.block_reason == "env_workspace_missing"
    assert blocked.continuation.wip is not None
    assert blocked.continuation.wip.ref == "ab33ee0"
    async with db.snapshot() as snap:
        rows = [i for i in (await inbox(snap)).items if i.subject_id == task.id]
    assert [r.state for r in rows] == ["blocked"]
