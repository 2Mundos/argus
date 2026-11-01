"""Dead-session recovery latency: session_lost → next claim (fleet health P5)."""

import json
import re
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api import fleet_health
from argus_api.agents import service as agents
from argus_api.db import Database, ts, utcnow
from argus_api.errors import ConflictError
from argus_api.fleet_health import (
    MIN_SAMPLES,
    RECOVERY_TARGET_S,
    SESSION_LOST_SQL,
    classify_recovery_row,
)
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.agents import AgentRegister, BootstrapTokenCreate
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

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


async def task_row(conn, task: str, state: str) -> None:
    """A minimal synthetic task row (its current state decides pending)."""
    await conn.execute(
        "INSERT OR REPLACE INTO tasks (id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project)"
        " VALUES (?, 'generic', ?, '', '[]', '{}', '[]', 0, ?, 0, 'none',"
        " 60, 3, 'test', ?, ?, 'global')",
        (task, f"title {task}", state, ts(NOW), ts(NOW)),
    )


async def session_lost(
    db: Database,
    task: str,
    lost_at: datetime,
    *,
    to: str = "ready",
    attempt: int = 1,
    next_claim_at: datetime | None = None,
    claim_attempt: int | None = None,
    state: str | None = None,
) -> None:
    """Synthetic session_lost fail (and optional next claim) — no production data."""
    async with db.write() as tx:
        default = "succeeded" if next_claim_at else ("ready" if to == "ready" else to)
        await task_row(tx.conn, task, state or default)
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
        await task_row(tx.conn, "order", "ready")
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
        "next_end_id": None,
        "next_claim_id": 2,
        "next_claim_at": ts(NOW - timedelta(seconds=5)),
        "task_state": "succeeded",
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
        "next_end_id": None,
        "next_claim_id": None,
        "next_claim_at": None,
        "task_state": "ready",
    }
    assert classify_recovery_row(row).outcome == "anomalous"


@pytest.mark.parametrize("state", ["ready", "pending", "blocked"])
async def test_overdue_counts_every_waiting_state(db: Database, state: str) -> None:
    """Each waiting state is pending; age counts time unclaimed, not work."""
    await session_lost(db, f"old-{state}", at(1), state=state)
    await session_lost(db, f"young-{state}", NOW - timedelta(seconds=10), state=state)
    r = await recovery(db)
    assert (r.pending, r.pending_overdue, r.measured, r.ended_unclaimed) == (2, 1, 0, 0)
    assert (r.p50, r.outliers) == (None, 0)


def test_pending_age_from_a_future_fail_is_not_overdue() -> None:
    """A clock rollback can leave a fail stamped after `now`: its age is negative."""
    row = {
        "id": 1,
        "at": ts(NOW + timedelta(seconds=RECOVERY_TARGET_S * 2)),
        "subject_id": "ahead",
        "data": json.dumps({"reason": "session_lost", "to": "ready"}),
        "next_end_id": None,
        "next_claim_id": None,
        "next_claim_at": None,
        "task_state": "ready",
    }
    r = fleet_health._dead_session_recovery([row], 24, NOW)
    assert (r.pending, r.pending_overdue, r.anomalous) == (1, 0, 0)


async def test_fail_stamped_after_now_leaves_the_window(db: Database) -> None:
    """Pinned limit (spec 3): the window reads the fail's `at`, so a fail written
    ahead of `now` during a clock rollback is excluded, not reported."""
    await session_lost(db, "ahead", NOW + timedelta(seconds=30))
    r = await recovery(db)
    assert (r.denominator, r.pending, r.anomalous) == (0, 0, 0)


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


# SQLite names a scan by the alias it was given (`SCAN e`, not `SCAN events`), so the
# guard matches every alias of events in the query, not only the table name.
EVENT_ALIASES = frozenset(
    {"events", *re.findall(r"\b(?:FROM|JOIN) events (\w+)", SESSION_LOST_SQL)}
)
# Each events lookup and the index it must use: the kind+at window, then the
# per-task correlated lookups (next end; claim, by the task+kind index of 0058). A
# kind-only lookup is a regression.
EXPECTED_EVENT_INDEXES = frozenset(
    {("e", "events_kind_at"), ("f", "events_subject"), ("c", "events_task_kind")}
)


# `SCAN e`, `SEARCH e USING ...`; older SQLite prints `SEARCH TABLE events AS e`.
PLAN_NODE = re.compile(r"(SCAN|SEARCH) (?:TABLE )?(\w+)(?: AS (\w+))?")


def plan_alias(node: re.Match[str]) -> str:
    return node.group(3) or node.group(2)


def event_scans(plan: list[str]) -> list[str]:
    """Full reads of events under any alias (a covering index scan counts too)."""
    return [
        line
        for line in plan
        if (node := PLAN_NODE.match(line))
        and node.group(1) == "SCAN"
        and plan_alias(node) in EVENT_ALIASES
    ]


def event_searches(plan: list[str]) -> set[tuple[str, str]]:
    """(alias, index) for each indexed SEARCH of events."""
    found = set()
    for line in plan:
        index = re.search(r"USING (?:COVERING )?INDEX (\w+)", line)
        node = PLAN_NODE.match(line)
        if (
            node
            and node.group(1) == "SEARCH"
            and index
            and plan_alias(node) in EVENT_ALIASES
        ):
            found.add((plan_alias(node), index.group(1)))
    return found


def plan_regressions(plan: list[str]) -> list[str]:
    missing = sorted(EXPECTED_EVENT_INDEXES - event_searches(plan))
    return [*event_scans(plan), *(f"no {a} via {i}" for a, i in missing)]


async def query_plan(db: Database) -> list[str]:
    since, until = ts(at(24)), ts(NOW)
    async with (
        db.read() as conn,
        conn.execute(
            "EXPLAIN QUERY PLAN " + SESSION_LOST_SQL, (since, until, 10)
        ) as cur,
    ):
        return [r["detail"] for r in await cur.fetchall()]


def test_plan_guard_flags_aliased_event_scans() -> None:
    """Synthetic plan lines: the old `"events" in line` check missed `SCAN e`."""
    assert event_scans(["SCAN e USING INDEX events_kind_at"]) == [
        "SCAN e USING INDEX events_kind_at"
    ]
    assert event_scans(["SCAN events"]) == ["SCAN events"]
    assert event_scans(["SCAN l", "SEARCH e USING INDEX events_kind_at (kind=?)"]) == []


async def test_session_lost_query_plan_uses_event_indexes(db: Database) -> None:
    plan = await query_plan(db)
    assert plan_regressions(plan) == [], plan


@pytest.mark.parametrize(
    "dropped", ["events_kind_at", "events_subject", "events_task_kind"]
)
async def test_missing_event_index_is_detected(db: Database, dropped: str) -> None:
    """Negative control: with the index gone, the same guard must fail."""
    async with db.write() as tx:
        await tx.conn.execute(f"DROP INDEX {dropped}")
    plan = await query_plan(db)
    assert plan_regressions(plan), plan


# -- real service paths (tasks.service via db.write, FakeClock as tx.now) ----------------


async def worker(db: Database) -> str:
    async with db.write() as tx:
        issued = await agents.create_bootstrap_token(
            tx, BootstrapTokenCreate(), "human:test"
        )
    async with db.write() as tx:
        reg = await agents.register(tx, AgentRegister(bootstrap_token=issued.token))
    return reg.agent.id


async def new_task(db: Database, title: str = "recover-me", **kw) -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="demo", title=title, **kw), "human:test"
        )
    return task.id


async def claim(db: Database, agent_id: str) -> tuple[str, int]:
    async with db.write() as tx:
        task = await tasks.claim(tx, agent_id, [])
    assert task is not None
    return task.id, task.attempt


async def lose(db: Database, agent_id: str, task_id: str, attempt: int) -> None:
    async with db.write() as tx:
        await tasks.fail(
            tx, agent_id, task_id, attempt, "container gone", reason="session_lost"
        )


async def report(db: Database, clock: FakeClock) -> fleet_health.DeadSessionRecovery:
    return (
        await fleet_health.fleet_health(db, 24, now=clock.now)
    ).dead_session_recovery


@pytest.mark.parametrize("step_back_s", [0, 5])
async def test_clock_rollback_claim_with_later_id_is_anomalous(
    db: Database, clock: FakeClock, step_back_s: int
) -> None:
    """Positive (0: forward clock) and negative (5 s rollback) control on the real
    claim/fail path: the claim always has the higher id; only its `at` moves."""
    agent = await worker(db)
    await new_task(db)
    task_id, attempt = await claim(db, agent)
    clock.advance(60)
    await lose(db, agent, task_id, attempt)
    lost_at = clock.now
    clock.now = lost_at + timedelta(seconds=30 if step_back_s == 0 else -step_back_s)
    await claim(db, agent)
    clock.now = lost_at + timedelta(minutes=1)
    r = await report(db, clock)
    assert r.denominator == 1 and r.pending == 0  # the claim is found either way
    if step_back_s == 0:
        assert (r.measured, r.anomalous) == (1, 0)
        assert r.examples[0].latency_s == pytest.approx(30)
    else:
        assert (r.measured, r.anomalous) == (0, 1)
        assert r.examples == []  # a negative latency never enters the distribution


async def test_pending_overdue_vs_fresh(db: Database, clock: FakeClock) -> None:
    agent = await worker(db)
    await new_task(db)
    task_id, attempt = await claim(db, agent)
    await lose(db, agent, task_id, attempt)
    lost_at = clock.now
    clock.now = lost_at + timedelta(seconds=RECOVERY_TARGET_S - 1)
    fresh = await report(db, clock)
    assert (fresh.pending, fresh.pending_overdue, fresh.measured) == (1, 0, 0)
    clock.now = lost_at + timedelta(seconds=RECOVERY_TARGET_S)
    overdue = await report(db, clock)
    assert (overdue.pending, overdue.pending_overdue) == (1, 1)
    # Censored: the age is no latency, so nothing is reported as one.
    assert overdue.n == 0 and overdue.outliers == 0
    assert (overdue.p50, overdue.p90, overdue.mean) == (None, None, None)


async def test_cancelled_after_fail_ended_unclaimed(
    db: Database, clock: FakeClock
) -> None:
    agent = await worker(db)
    await new_task(db)
    task_id, attempt = await claim(db, agent)
    await lose(db, agent, task_id, attempt)
    clock.advance(RECOVERY_TARGET_S * 2)
    async with db.write() as tx:
        await tasks.cancel(tx, task_id, "human:test")
    r = await report(db, clock)
    assert (r.ended_unclaimed, r.pending, r.pending_overdue, r.measured) == (1, 0, 0, 0)
    assert r.coverage == 0.0


async def test_retry_sequence_pairs_each_fail_with_its_own_claim(
    db: Database, clock: FakeClock
) -> None:
    agent = await worker(db)
    await new_task(db, max_attempts=5)
    task_id, attempt = await claim(db, agent)
    for gap in (20, 40, None):
        clock.advance(10)
        # progress lets a later confirmed exit resume again (failure classes 3.2)
        async with db.write() as tx:
            await tasks.report_progress(tx, agent, task_id, attempt, "working")
        await lose(db, agent, task_id, attempt)
        if gap is None:
            break  # third loss: not claimed again yet
        clock.advance(gap)
        _, attempt = await claim(db, agent)
    r = await report(db, clock)
    assert (r.denominator, r.measured, r.pending, r.anomalous) == (3, 2, 1, 0)
    assert sorted(s.latency_s for s in r.examples) == [20.0, 40.0]
    assert len({s.claim_event_id for s in r.examples}) == 2


async def test_exhausted_then_operator_retry_is_not_requeued(
    db: Database, clock: FakeClock
) -> None:
    agent = await worker(db)
    await new_task(db, max_attempts=1)
    task_id, attempt = await claim(db, agent)
    await lose(db, agent, task_id, attempt)
    async with db.write() as tx:
        await tasks.retry(tx, task_id, "human:test")
    clock.advance(5)
    await claim(db, agent)
    r = await report(db, clock)
    assert (r.not_requeued, r.measured, r.pending) == (1, 0, 0)


async def test_later_fail_before_any_claim_does_not_steal_the_pairing(
    db: Database,
) -> None:
    """Two fails, then one claim: only the second fail owns it (synthetic, since a
    real fail needs a claim first)."""
    lost = at(1)
    async with db.write() as tx:
        await task_row(tx.conn, "steal", "succeeded")
        for event_id, seconds in ((10, 0), (11, 30)):
            await event(
                tx.conn,
                "steal",
                "task.fail",
                lost + timedelta(seconds=seconds),
                event_id=event_id,
                reason="session_lost",
                **{"from": "claimed", "to": "ready"},
            )
        await event(
            tx.conn, "steal", "task.claim", lost + timedelta(seconds=50), event_id=12
        )
    r = await recovery(db)
    assert (r.denominator, r.measured, r.anomalous) == (2, 1, 1)
    assert r.examples[0].fail_event_id == 11 and r.examples[0].latency_s == 20.0


async def test_expire_bounds_the_pairing_too(db: Database) -> None:
    lost = at(1)
    async with db.write() as tx:
        await task_row(tx.conn, "exp", "ready")
        await event(
            tx.conn,
            "exp",
            "task.fail",
            lost,
            event_id=20,
            reason="session_lost",
            **{"from": "claimed", "to": "ready"},
        )
        await event(
            tx.conn, "exp", "task.expire", lost + timedelta(seconds=5), event_id=21
        )
        await event(
            tx.conn, "exp", "task.claim", lost + timedelta(seconds=9), event_id=22
        )
    r = await recovery(db)
    assert (r.measured, r.anomalous) == (0, 1)


@pytest.mark.parametrize("state", ["claimed", "in_review", None])
def test_no_paired_claim_with_running_or_missing_task_is_anomalous(
    state: str | None,
) -> None:
    row = {
        "id": 1,
        "at": ts(NOW),
        "subject_id": "gone",
        "data": json.dumps({"reason": "session_lost", "to": "ready"}),
        "next_end_id": None,
        "next_claim_id": None,
        "next_claim_at": None,
        "task_state": state,
    }
    assert classify_recovery_row(row).outcome == "anomalous"


@pytest.mark.parametrize(
    ("lost_at", "claim_at"),
    [
        ("not-a-time", None),
        (None, None),
        ("2026-03-01T00:00:00", None),  # no zone: cannot be compared
        (ts(NOW), "garbage"),
    ],
)
def test_unknown_or_unreadable_time_is_anomalous(
    lost_at: str | None, claim_at: str | None
) -> None:
    row = {
        "id": 1,
        "at": lost_at,
        "subject_id": "t",
        "data": json.dumps({"reason": "session_lost", "to": "ready"}),
        "next_end_id": None,
        "next_claim_id": 2 if claim_at else None,
        "next_claim_at": claim_at,
        "task_state": "ready",
    }
    sample = classify_recovery_row(row)
    assert sample.outcome == "anomalous" and sample.latency_s is None


async def test_truncation_is_flagged(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3):
        await session_lost(db, f"many{i}", at(1, seconds=i))
    monkeypatch.setattr(fleet_health, "MAX_ROWS", 2)
    r = await recovery(db)
    assert r.truncated and r.denominator == 2
    monkeypatch.setattr(fleet_health, "MAX_ROWS", 3 + 1)
    assert not (await recovery(db)).truncated


# -- HTTP route ------------------------------------------------------------------------

ADMIN_TOKEN = "dead-session-admin-token-" + "r" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def seed_pending(client: TestClient) -> None:
    """One overdue and one fresh pending fail, one cancelled after its fail."""

    async def seed() -> None:
        now = utcnow()
        async with client.app.state.db.write() as tx:
            for task, state, ago in (
                ("overdue", "ready", RECOVERY_TARGET_S + 60),
                ("fresh", "ready", 10),
                ("dropped", "cancelled", 600),
            ):
                await task_row(tx.conn, task, state)
                await event(
                    tx.conn,
                    task,
                    "task.fail",
                    now - timedelta(seconds=ago),
                    reason="session_lost",
                    **{"from": "claimed", "to": "ready"},
                )

    client.portal.call(seed)


def test_route_reports_pending_overdue_and_ended_unclaimed(client: TestClient) -> None:
    seed_pending(client)
    res = client.get(
        "/api/fleet/health", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )
    assert res.status_code == 200
    d = res.json()["dead_session_recovery"]
    assert (d["denominator"], d["pending"], d["pending_overdue"]) == (3, 2, 1)
    assert (d["ended_unclaimed"], d["measured"], d["truncated"]) == (1, 0, False)
    assert d["p50"] is None and d["outliers"] == 0


async def test_requeued_task_blocked_for_quota_counts_as_pending(
    db: Database, clock: FakeClock
) -> None:
    """A requeued task can reach `blocked` with no claim (ready -> blocked, quota
    wait), and `pending` then mixes it with tasks waiting for a worker."""
    agent = await worker(db)
    task = await new_task(db)
    task_id, attempt = await claim(db, agent)
    await lose(db, agent, task_id, attempt)
    async with db.write() as tx:
        assert await tasks.await_quota_ready(tx, agent, []) == [task]
    clock.advance(RECOVERY_TARGET_S * 2)
    async with db.read() as conn, conn.execute("SELECT state FROM tasks") as cur:
        assert [r["state"] for r in await cur.fetchall()] == ["blocked"]
    r = await report(db, clock)
    assert (r.pending, r.pending_overdue, r.measured) == (1, 1, 0)


async def test_cancelled_task_cannot_be_retried_into_a_pairing(
    db: Database, clock: FakeClock
) -> None:
    agent = await worker(db)
    await new_task(db)
    task_id, attempt = await claim(db, agent)
    await lose(db, agent, task_id, attempt)
    async with db.write() as tx:
        await tasks.cancel(tx, task_id, "human:test")
    with pytest.raises(ConflictError, match="not failed"):
        async with db.write() as tx:
            await tasks.retry(tx, task_id, "human:test")
    assert (await report(db, clock)).ended_unclaimed == 1
