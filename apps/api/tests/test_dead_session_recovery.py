"""Dead-session recovery latency: session_lost → next claim (fleet health P5)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from argus_api import fleet_health
from argus_api.db import Database, ts
from argus_api.fleet_health import (
    MIN_SAMPLES,
    RECOVERY_TARGET_S,
    SESSION_LOST_SQL,
    classify_recovery_row,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def at(hours_ago: float, seconds: float = 0) -> datetime:
    return NOW - timedelta(hours=hours_ago) + timedelta(seconds=seconds)


async def event(
    conn, task: str, kind: str, when: datetime, *, event_id: int | None = None, **data
) -> int:
    if event_id is None:
        cur = await conn.execute(
            "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
            " VALUES (?, 'system', 'task', ?, ?, ?)",
            (ts(when), task, kind, json.dumps(data)),
        )
        return int(cur.lastrowid)
    await conn.execute(
        "INSERT INTO events (id, at, actor, subject_type, subject_id, kind, data)"
        " VALUES (?, ?, 'system', 'task', ?, ?, ?)",
        (event_id, ts(when), task, kind, json.dumps(data)),
    )
    return event_id


async def session_lost(
    db: Database,
    task: str,
    lost_at: datetime,
    *,
    to: str = "ready",
    attempt: int = 1,
    next_claim_at: datetime | None = None,
    claim_attempt: int | None = None,
) -> None:
    """Synthetic session_lost fail (and optional next claim) — no production data."""
    async with db.write() as tx:
        await event(
            tx.conn,
            task,
            "task.fail",
            lost_at,
            reason="session_lost",
            error="container gone",
            retry_class="session",
            attempt=attempt,
            **{"from": "claimed", "to": to},
        )
        if next_claim_at is not None:
            await event(
                tx.conn,
                task,
                "task.claim",
                next_claim_at,
                attempt=claim_attempt if claim_attempt is not None else attempt + 1,
            )


async def recovery(db: Database, window: int = 24) -> fleet_health.DeadSessionRecovery:
    return (await fleet_health.fleet_health(db, window, now=NOW)).dead_session_recovery


# -- positive / negative controls -------------------------------------------------------


async def test_positive_control_measures_known_gap(db: Database) -> None:
    lost = at(1)
    await session_lost(db, "ok", lost, next_claim_at=lost + timedelta(seconds=45))
    # Pad to MIN_SAMPLES with other known gaps so percentiles are defined.
    for i, gap in enumerate([30, 60, 90, 120]):
        t0 = at(2, seconds=i)
        await session_lost(db, f"ok{i}", t0, next_claim_at=t0 + timedelta(seconds=gap))
    r = await recovery(db)
    assert r.denominator == MIN_SAMPLES
    assert r.measured == MIN_SAMPLES
    assert r.coverage == 1.0
    assert r.failure_class == "session" and r.reason == "session_lost"
    assert r.p50 == pytest.approx(60)
    assert r.mean == pytest.approx(69)  # (45+30+60+90+120)/5
    assert r.outliers == 0
    assert r.target_s == RECOVERY_TARGET_S
    assert r.examples[0].latency_s is not None
    assert r.examples[0].fail_event_id > 0
    assert r.examples[0].claim_event_id is not None


async def test_negative_control_other_reasons_are_excluded(db: Database) -> None:
    lost = at(1)
    async with db.write() as tx:
        await event(
            tx.conn,
            "other",
            "task.fail",
            lost,
            reason="worker_fail",
            error="boom",
            **{"from": "claimed", "to": "ready"},
        )
        await event(tx.conn, "other", "task.claim", lost + timedelta(seconds=10))
        await event(
            tx.conn,
            "stalled",
            "task.expire",
            lost,
            reason="stalled",
            **{"from": "claimed", "to": "ready"},
        )
        await event(tx.conn, "stalled", "task.claim", lost + timedelta(seconds=10))
    r = await recovery(db)
    assert (r.denominator, r.measured, r.coverage) == (0, 0, None)
    assert (r.p50, r.p90, r.mean) == (None, None, None)


# -- retries, ordering, sparse, pending, terminal ---------------------------------------


async def test_retries_on_same_task_are_independent_samples(db: Database) -> None:
    t0 = at(3)
    async with db.write() as tx:
        await event(
            tx.conn,
            "retry",
            "task.fail",
            t0,
            reason="session_lost",
            attempt=1,
            **{"from": "claimed", "to": "ready"},
        )
        await event(
            tx.conn, "retry", "task.claim", t0 + timedelta(seconds=20), attempt=2
        )
        await event(
            tx.conn,
            "retry",
            "task.fail",
            t0 + timedelta(seconds=100),
            reason="session_lost",
            attempt=2,
            **{"from": "claimed", "to": "ready"},
        )
        await event(
            tx.conn, "retry", "task.claim", t0 + timedelta(seconds=140), attempt=3
        )
    r = await recovery(db)
    assert r.denominator == 2 and r.measured == 2
    assert sorted(s.latency_s for s in r.examples) == [20.0, 40.0]
    assert r.p50 is None  # sparse: below MIN_SAMPLES


async def test_sparse_window_keeps_counts_but_null_percentiles(db: Database) -> None:
    for i in range(MIN_SAMPLES - 1):
        t0 = at(1, seconds=i)
        await session_lost(
            db, f"sparse{i}", t0, next_claim_at=t0 + timedelta(seconds=10 * (i + 1))
        )
    r = await recovery(db)
    assert r.measured == MIN_SAMPLES - 1
    assert r.coverage == 1.0
    assert (r.p50, r.p90, r.mean) == (None, None, None)
    assert r.n == MIN_SAMPLES - 1


async def test_pending_requeue_without_next_claim_is_not_measured(db: Database) -> None:
    await session_lost(db, "waiting", at(1), to="ready", next_claim_at=None)
    r = await recovery(db)
    assert (r.denominator, r.measured, r.pending) == (1, 0, 1)
    assert r.coverage == 0.0
    assert r.p50 is None


async def test_exhausted_attempts_are_not_requeued(db: Database) -> None:
    await session_lost(db, "done", at(1), to="failed", next_claim_at=None)
    r = await recovery(db)
    assert (r.denominator, r.measured, r.not_requeued) == (1, 0, 1)
    assert r.coverage == 0.0


async def test_claim_before_fail_is_not_paired_as_next(db: Database) -> None:
    lost = at(1)
    async with db.write() as tx:
        await event(
            tx.conn, "order", "task.claim", lost - timedelta(seconds=30), attempt=1
        )
        await event(
            tx.conn,
            "order",
            "task.fail",
            lost,
            reason="session_lost",
            attempt=1,
            **{"from": "claimed", "to": "ready"},
        )
    r = await recovery(db)
    assert (r.measured, r.pending) == (0, 1)


async def test_same_instant_claim_with_higher_id_is_measured_zero(
    db: Database,
) -> None:
    lost = at(1)
    async with db.write() as tx:
        await event(
            tx.conn,
            "tie",
            "task.fail",
            lost,
            event_id=100,
            reason="session_lost",
            **{"from": "claimed", "to": "ready"},
        )
        await event(tx.conn, "tie", "task.claim", lost, event_id=101, attempt=2)
    r = await recovery(db)
    assert r.measured == 1
    assert r.examples[0].latency_s == 0.0


# -- clock / anomalous / outliers -------------------------------------------------------


def test_classify_negative_latency_is_anomalous() -> None:
    row = {
        "id": 1,
        "at": ts(NOW),
        "subject_id": "clock",
        "data": json.dumps({"reason": "session_lost", "to": "ready"}),
        "next_claim_id": 2,
        "next_claim_at": ts(NOW - timedelta(seconds=5)),
    }
    sample = classify_recovery_row(row)
    assert sample.outcome == "anomalous"
    assert sample.latency_s == pytest.approx(-5.0)


def test_classify_unreadable_payload_is_anomalous() -> None:
    row = {
        "id": 1,
        "at": ts(NOW),
        "subject_id": "bad",
        "data": "{not-json",
        "next_claim_id": None,
        "next_claim_at": None,
    }
    assert classify_recovery_row(row).outcome == "anomalous"


async def test_outliers_count_at_or_above_target_and_still_enter_median(
    db: Database,
) -> None:
    gaps = [10, 20, 30, 40, RECOVERY_TARGET_S + 60]
    for i, gap in enumerate(gaps):
        t0 = at(1, seconds=i)
        await session_lost(db, f"out{i}", t0, next_claim_at=t0 + timedelta(seconds=gap))
    r = await recovery(db)
    assert r.outliers == 1
    assert r.measured == MIN_SAMPLES
    assert r.p50 is not None  # outlier remains in the distribution


async def test_window_excludes_old_session_lost(db: Database) -> None:
    old = at(25)
    await session_lost(db, "old", old, next_claim_at=old + timedelta(seconds=5))
    recent = at(1)
    await session_lost(db, "new", recent, next_claim_at=recent + timedelta(seconds=5))
    r = await recovery(db, 24)
    assert r.denominator == 1 and r.examples[0].task_id == "new"


async def test_session_lost_query_uses_kind_at_index(db: Database) -> None:
    since, until = ts(at(24)), ts(NOW)
    async with (
        db.read() as conn,
        conn.execute(
            "EXPLAIN QUERY PLAN " + SESSION_LOST_SQL, (since, until, 10)
        ) as cur,
    ):
        plan = [r["detail"] for r in await cur.fetchall()]
    assert any("events_kind_at" in line for line in plan)
    for line in plan:
        assert not (line.startswith("SCAN") and "events" in line), plan
