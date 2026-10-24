"""GET /api/fleet/health: windows, MIN_SAMPLES, percentiles, failures, indexes."""

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from argus_api import fleet_health
from argus_api.db import Database, ts
from argus_api.fleet_health import (
    ATTEMPTS_SQL,
    FAILURES_SQL,
    FINISHED_SQL,
    HEARTBEAT_SQL,
    MIN_SAMPLES,
    USAGE_SQL,
)
from argus_api.main import app
from argus_core.tasks import TaskState
from fastapi.testclient import TestClient

NOW = datetime(2026, 3, 1, tzinfo=UTC)
ADMIN_TOKEN = "fleet-health-admin-token-" + "v" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def at(hours_ago: float, seconds: float = 0) -> str:
    return ts(NOW - timedelta(hours=hours_ago) + timedelta(seconds=seconds))


async def add_finished(
    db: Database,
    n: str,
    state: str = "succeeded",
    *,
    ended_hours_ago: float = 1,
    queue_s: float | None = 10,
    run_s: float | None = 20,
) -> None:
    """A finished task created `queue_s` before its claim and ended `run_s` after."""
    end = NOW - timedelta(hours=ended_hours_ago)
    claim = end - timedelta(seconds=run_s or 0)
    created = claim - timedelta(seconds=queue_s or 0)
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project)"
            " VALUES (?, 'generic', ?, '', '[]', '{}', '[]', 0, ?, 0, 'none',"
            " 60, 3, 'test', ?, ?, 'global')",
            (n, f"title {n}", state, ts(created), ts(end)),
        )
        if run_s is not None:
            await event(tx.conn, n, "task.claim", claim)


async def event(conn, task: str, kind: str, when: datetime, **data) -> None:
    await conn.execute(
        "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
        " VALUES (?, 'system', 'task', ?, ?, ?)",
        (ts(when), task, kind, json.dumps(data)),
    )


async def add_failure(
    db: Database, n: str, kind: str, hours_ago: float, **data: str
) -> None:
    await add_finished(db, n, "failed", ended_hours_ago=hours_ago)
    async with db.write() as tx:
        await event(tx.conn, n, kind, NOW - timedelta(hours=hours_ago), **data)


async def add_pool(db: Database, name: str, desired: int = 1) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO pools (name, target, runtime, image, capabilities,"
            " desired_replicas, max_replicas, config, status, created_at, updated_at)"
            " VALUES (?, 'docker', 'plain', 'img', '[]', ?, 5, '{}', 'ok', ?, ?)",
            (name, desired, ts(NOW), ts(NOW)),
        )


async def add_instance_agent(
    db: Database, pool: str, iid: str, state: str, beat: str, agent_state="active"
) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO instances (id, pool, provider_ref, state, created_at,"
            " terminated_at) VALUES (?, ?, 'ref', ?, ?, ?)",
            (iid, pool, state, ts(NOW), ts(NOW) if state == "terminated" else None),
        )
        await tx.conn.execute(
            "INSERT INTO agents (id, name, role, instance_id, capabilities, state,"
            " last_heartbeat_at, created_at, ended_at)"
            " VALUES (?, ?, 'worker', ?, '[]', ?, ?, ?, ?)",
            (
                f"a-{iid}",
                iid,
                iid,
                agent_state,
                beat,
                ts(NOW),
                ts(NOW) if agent_state in ("gone", "lost") else None,
            ),
        )


async def health(db: Database, window: int = 24) -> fleet_health.FleetHealth:
    return await fleet_health.fleet_health(db, window, now=NOW)


# -- pools ----------------------------------------------------------------------------


async def test_empty_pool_is_listed_with_explicit_nulls(db: Database) -> None:
    await add_pool(db, "empty", desired=2)
    h = await health(db)
    (pool,) = h.pools
    assert (pool.name, pool.desired, pool.live, pool.running) == ("empty", 2, 0, 0)
    assert pool.last_heartbeat_at is None
    assert "last_heartbeat_at" in pool.model_dump()


async def test_pool_live_running_and_latest_live_heartbeat(db: Database) -> None:
    await add_pool(db, "p")
    await add_instance_agent(db, "p", "i1", "running", at(2))
    await add_instance_agent(db, "p", "i2", "running", at(1))
    await add_instance_agent(db, "p", "i3", "terminating", at(0.5), agent_state="lost")
    await add_instance_agent(db, "p", "i4", "terminated", at(0.1), agent_state="gone")
    (pool,) = (await health(db)).pools
    assert (pool.live, pool.running) == (3, 2)
    assert pool.last_heartbeat_at == NOW - timedelta(hours=1)  # lost/gone ignored


# -- window ---------------------------------------------------------------------------


async def test_window_boundaries_and_old_tasks_excluded(db: Database) -> None:
    for i in range(MIN_SAMPLES):
        await add_finished(db, f"in{i}", ended_hours_ago=24)  # exactly on the edge
    await add_finished(db, "old", ended_hours_ago=24.01)
    await add_failure(db, "oldfail", "task.fail", 50, reason="worker_fail", error="x")
    h = await health(db, 24)
    assert h.outcomes.total == MIN_SAMPLES
    assert h.outcomes.failed == 0
    assert h.failures.total == 0
    assert (await health(db, 72)).failures.total == 1
    assert h.outcomes.window_hours == h.queue_time.window_hours == 24


async def test_tasks_by_state_has_every_state_and_ignores_the_window(
    db: Database,
) -> None:
    await add_finished(db, "old", ended_hours_ago=1000)
    h = await health(db)
    assert set(h.tasks_by_state.counts) == {s.value for s in TaskState}
    assert h.tasks_by_state.counts["succeeded"] == 1
    assert h.tasks_by_state.source == "tasks"


# -- MIN_SAMPLES and percentiles -------------------------------------------------------


async def test_fewer_than_min_samples_is_null_never_zero(db: Database) -> None:
    for i in range(MIN_SAMPLES - 1):
        await add_finished(db, f"t{i}", "succeeded" if i else "failed")
    h = await health(db)
    for d in (h.queue_time, h.run_time):
        assert (d.n, d.p50, d.p90, d.mean) == (MIN_SAMPLES - 1, None, None, None)
    assert h.outcomes.total == MIN_SAMPLES - 1
    assert (h.outcomes.success_rate, h.outcomes.failure_rate) == (None, None)
    assert h.outcomes.failed == 1  # counts stay real


async def test_empty_window_reports_null_with_n_zero(db: Database) -> None:
    h = await health(db)
    assert (h.queue_time.n, h.queue_time.p50, h.run_time.mean) == (0, None, None)


async def test_percentiles_and_mean(db: Database) -> None:
    for i, run in enumerate([10, 20, 30, 40, 100]):
        await add_finished(db, f"t{i}", queue_s=run * 2, run_s=run)
    h = await health(db)
    assert (h.run_time.n, h.run_time.p50, h.run_time.mean) == (5, 30, 40)
    assert h.run_time.p90 == pytest.approx(76)  # 40 + 0.6 * (100 - 40)
    assert h.queue_time.p50 == 60


async def test_unclaimed_finished_tasks_have_no_durations(db: Database) -> None:
    for i in range(MIN_SAMPLES):
        await add_finished(db, f"t{i}", run_s=None)
    h = await health(db)
    assert (h.run_time.n, h.queue_time.n, h.outcomes.total) == (0, 0, MIN_SAMPLES)
    assert h.outcomes.success_rate == 1.0


async def test_outcome_rates(db: Database) -> None:
    for i in range(3):
        await add_finished(db, f"s{i}")
    for i in range(2):
        await add_finished(db, f"f{i}", "failed")
    o = (await health(db)).outcomes
    assert (o.succeeded, o.failed) == (3, 2)
    assert (o.success_rate, o.failure_rate) == (0.6, 0.4)


# -- failures ---------------------------------------------------------------------------


def test_classify_failure() -> None:
    c = fleet_health.classify_failure
    assert c("session_lost", "container gone") == "session_lost"
    assert c("lease_expired", "x") == "lease_expired"
    assert c("timeout", None) == "timeout"
    assert c("agent_ended", None) == "agent_ended"
    assert c("provider_failure", "x") == "provider_failure"
    assert c("worker_fail", "Run blocked: AUTH_MISSING") == "auth_missing"
    assert c("worker_fail", "cli not_installed: codex") == "not_installed"
    assert c("worker_fail", "gate_failed: lint") == "gate_failed"
    assert c("worker_fail", "boom") == "worker_fail"
    assert c(None, "boom") == "other"
    assert c("surprise", "auth_missing") == "other"


async def test_failures_group_by_reason_with_links_and_excerpts(db: Database) -> None:
    for i in range(7):
        await add_failure(
            db, f"lease{i}", "task.expire", 10 - i, reason="lease_expired"
        )
    await add_failure(
        db, "auth", "task.fail", 2, reason="worker_fail", error="auth_missing"
    )
    await add_failure(db, "long", "task.fail", 3, reason="worker_fail", error="e" * 500)
    await add_failure(db, "none", "task.fail", 4)
    f = (await health(db)).failures
    assert f.total == 10
    assert [(g.reason, g.count) for g in f.groups] == [
        ("lease_expired", 7),
        ("auth_missing", 1),
        ("other", 1),
        ("worker_fail", 1),
    ]
    assert f.by_class == {
        "admission": 0,
        "environment": 0,
        "transient": 0,
        "session": 7,
        "task": 3,
    }
    lease = f.groups[0]
    assert len(lease.examples) == 5
    assert [e.task_id for e in lease.examples][:2] == ["lease6", "lease5"]  # newest
    assert lease.examples[0].title == "title lease6"
    worker = next(g for g in f.groups if g.reason == "worker_fail")
    assert len(worker.examples[0].error) == 200
    assert f.source == "events task.fail, task.expire"


# -- no full scans ------------------------------------------------------------------------


async def test_tasks_by_state_is_a_covering_index_count(db: Database) -> None:
    # A snapshot count must visit every row; it never reads the table itself.
    async with db.read() as conn:
        sql = "EXPLAIN QUERY PLAN SELECT state, count(*) FROM tasks GROUP BY state"
        async with conn.execute(sql) as cur:
            (row,) = await cur.fetchall()
    assert "COVERING INDEX tasks_state" in row["detail"]


async def test_main_queries_use_indexes(db: Database) -> None:
    since, until = at(24), at(0)
    queries = {
        "finished": (FINISHED_SQL, (since, until, 10)),
        "failures": (FAILURES_SQL, (since, until, 10)),
        "heartbeat": (HEARTBEAT_SQL, ()),
        "attempts": (ATTEMPTS_SQL, (since, until)),
        "usage": (USAGE_SQL, ("[]",)),
    }
    async with db.read() as conn:
        for name, (sql, params) in queries.items():
            async with conn.execute("EXPLAIN QUERY PLAN " + sql, params) as cur:
                plan = [r["detail"] for r in await cur.fetchall()]
            for line in plan:
                assert not (
                    line.startswith("SCAN")
                    and ("tasks" in line or "events" in line or "account_usage" in line)
                ), (
                    name,
                    plan,
                )


# -- HTTP ------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def test_scopes_window_validation_and_cap(client: TestClient) -> None:
    assert client.get("/api/fleet/health").status_code == 401
    boot = client.post(
        "/api/agents/bootstrap-tokens", headers=ADMIN, json={"role": "worker"}
    ).json()["token"]
    worker = client.post("/api/agents/register", json={"bootstrap_token": boot}).json()[
        "token"
    ]
    assert (
        client.get(
            "/api/fleet/health", headers={"Authorization": f"Bearer {worker}"}
        ).status_code
        == 403
    )
    ok = client.get("/api/fleet/health", headers=ADMIN)
    assert ok.status_code == 200
    assert ok.json()["window_hours"] == 24
    assert client.get("/api/fleet/health?window=720", headers=ADMIN).status_code == 200
    for bad in ("721", "0", "-1", "abc"):
        r = client.get(f"/api/fleet/health?window={bad}", headers=ADMIN)
        assert r.status_code == 422, bad


async def add_usage(db: Database, task: str, tokens: int, cost: float) -> None:
    async with db.write() as tx:
        await tx.conn.execute(
            "INSERT INTO accounts (id, name, provider, kind, secret_ref, priority,"
            " state, created_at, updated_at) SELECT 'acc', 'acc', 'anthropic',"
            " 'api_key', 'sops:acc', 0, 'available', ?, ? WHERE NOT EXISTS"
            " (SELECT 1 FROM accounts WHERE id = 'acc')",
            (ts(NOW), ts(NOW)),
        )
        await tx.conn.execute(
            "INSERT INTO account_usage (account_id, task_id, input_tokens,"
            " output_tokens, cost_usd, at) VALUES ('acc', ?, ?, 0, ?, ?)",
            (task, tokens, cost, ts(NOW)),
        )


async def test_yield_counts_attempts_tokens_per_outcome_and_lost_time(
    db: Database,
) -> None:
    for i in range(MIN_SAMPLES):
        await add_finished(db, f"ok{i}", "succeeded")
        await add_usage(db, f"ok{i}", 1000 * (i + 1), 0.5)
    for i in range(MIN_SAMPLES):
        await add_failure(db, f"bad{i}", "task.expire", 1, reason="lease_expired")
    await add_usage(db, "bad0", 7, 0.1)  # one covered failure: below MIN_SAMPLES
    async with db.write() as tx:
        await event(tx.conn, "ok0", "task.complete", NOW - timedelta(hours=1))
    h = await health(db)
    y = h.yield_
    assert (y.attempts, y.completions) == (2 * MIN_SAMPLES, 1)
    assert y.finished_tasks == 2 * MIN_SAMPLES
    assert y.usage_tasks == MIN_SAMPLES + 1
    assert y.tokens_per_succeeded == 3000.0 and y.cost_per_succeeded == 0.5
    assert y.tokens_per_failed is None  # never a mean of one task
    (group,) = [g for g in h.failures.groups if g.reason == "lease_expired"]
    assert group.lost_seconds == 20.0 * MIN_SAMPLES
    assert h.model_dump(by_alias=True)["yield"]["attempts"] == 2 * MIN_SAMPLES


def test_classify_failure_tolerates_spaces_and_hyphens() -> None:
    assert (
        fleet_health.classify_failure("worker_fail", "Auth missing: no token")
        == "auth_missing"
    )
    assert (
        fleet_health.classify_failure("worker_fail", "gate-failed (lint)")
        == "gate_failed"
    )
    assert fleet_health.classify_failure("worker_fail", "boom") == "worker_fail"
