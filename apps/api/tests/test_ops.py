import asyncio
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api import ops, reaper
from argus_api.db import Database
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

ADMIN_TOKEN = "ops-admin-token-" + "o" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def fresh_loop(name: str = "reaper", interval: float = 10) -> ops.LoopStats:
    s = ops.LoopStats(name, interval)
    s.record(0.01)
    return s


# -- readiness ------------------------------------------------------------------------


async def test_ready_when_everything_is_fine(db: Database, tmp_path: Path) -> None:
    r = await ops.readiness(db, {"reaper": fresh_loop()}, tmp_path, secrets_ok=True)
    assert r.ready
    assert {k: c.status for k, c in r.checks.items()} == {
        "database": "ok",
        "reaper": "ok",
        "disk": "ok",
        "secrets": "ok",
    }


async def test_a_wedged_writer_makes_it_unready(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "WRITE_PROBE_TIMEOUT_S", 0.2)
    async with db.write():  # someone holds the single writer
        assert (await ops.readiness(db, {}, tmp_path, secrets_ok=True)).ready
        await asyncio.sleep(0.3)
        r = await ops.readiness(db, {}, tmp_path, secrets_ok=True)
    assert not r.ready
    assert r.checks["database"].status == "failing"
    assert "writer busy" in r.checks["database"].detail
    # …and it recovers as soon as the writer is free again.
    assert (await ops.readiness(db, {}, tmp_path, secrets_ok=True)).ready


async def test_readiness_never_queues_on_the_writer(
    db: Database, tmp_path: Path
) -> None:
    """Anonymous probes must not wait for, hold, or delay the single writer."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with db.write():
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await entered.wait()
    waiter = asyncio.create_task(_write_once(db))  # a real writer queued behind
    await asyncio.sleep(0)
    start = time.monotonic()
    results = await asyncio.wait_for(
        asyncio.gather(
            *(ops.readiness(db, {}, tmp_path, secrets_ok=True) for _ in range(50))
        ),
        timeout=ops.WRITE_PROBE_TIMEOUT_S / 2,
    )
    assert time.monotonic() - start < ops.WRITE_PROBE_TIMEOUT_S / 2
    assert all(r.ready for r in results)
    release.set()
    await holder
    # The queued writer still gets its turn once the holder lets go.
    await asyncio.wait_for(waiter, timeout=2)


async def _write_once(db: Database) -> None:
    async with db.write():
        pass


async def test_readiness_cache_runs_one_probe_per_window() -> None:
    cache = ops.ReadinessCache(ttl_s=60)
    calls = 0

    async def probe() -> ops.Readiness:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return ops.Readiness({"database": ops.Check("ok")})

    results = await asyncio.gather(*(cache.get(probe) for _ in range(100)))
    await cache.get(probe)
    assert calls == 1
    assert all(r is results[0] for r in results)
    expired = ops.ReadinessCache(ttl_s=0)
    await expired.get(probe)
    await expired.get(probe)
    assert calls == 3


async def test_a_stalled_loop_is_failing_and_a_new_one_gets_a_grace_window(
    db: Database, tmp_path: Path
) -> None:
    stalled = ops.LoopStats("reconciler", interval_s=5)
    stalled.started_at = time.monotonic() - 3600  # started long ago, never passed
    brand_new = ops.LoopStats("reaper", interval_s=5)  # no pass yet, just started
    r = await ops.readiness(
        db, {"reconciler": stalled, "reaper": brand_new}, tmp_path, secrets_ok=True
    )
    assert r.checks["reconciler"].status == "failing"
    assert r.checks["reaper"].status == "ok"
    assert not r.ready


async def test_stale_window_scales_with_the_interval() -> None:
    assert ops.LoopStats("x", 5).stale_after_s == 30  # floor
    assert ops.LoopStats("x", 60).stale_after_s == 180


async def test_low_disk_is_failing_and_missing_secrets_only_degrade(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "disk_free", lambda _p: 1024)
    r = await ops.readiness(db, {}, tmp_path, secrets_ok=False)
    assert r.checks["disk"].status == "failing"
    assert r.checks["secrets"].status == "degraded"
    monkeypatch.setattr(ops, "disk_free", lambda _p: 10**12)
    degraded_only = await ops.readiness(db, {}, tmp_path, secrets_ok=False)
    assert degraded_only.ready  # degraded is still ready


# -- loop heartbeats --------------------------------------------------------------------


async def test_the_reaper_records_passes_and_failures(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = ops.LoopStats("reaper", 0.01)
    task = asyncio.create_task(reaper.run(db, interval=0.01, stats=stats))
    await asyncio.sleep(0.15)
    assert stats.passes_total >= 2 and stats.errors_total == 0
    assert stats.last_success_unix is not None

    async def boom(_tx):
        raise RuntimeError("nope")

    monkeypatch.setattr(reaper, "reap", boom)
    # A pass already in flight may still finish well; wait for the first failure
    # before taking the baseline.
    for _ in range(100):
        if stats.errors_total >= 1:
            break
        await asyncio.sleep(0.02)
    errors, before = stats.errors_total, stats.last_success_unix
    assert errors >= 1
    await asyncio.sleep(0.15)
    task.cancel()
    assert stats.errors_total > errors
    assert stats.last_success_unix == before  # failures do not look like progress


# -- metrics ----------------------------------------------------------------------------


async def test_snapshot_reports_queue_age(
    db: Database, clock: FakeClock, tmp_path: Path
) -> None:
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="global", title="old"), "t")
    clock.advance(90)
    snap = await ops.snapshot(db, tmp_path / "argus.db", tmp_path, now=clock.now)
    assert snap.tasks["ready"] == 1
    assert snap.tasks["succeeded"] == 0  # every state is present
    assert snap.oldest_ready_age_s == pytest.approx(90)
    assert snap.events_total >= 1


def test_label_values_are_escaped() -> None:
    assert ops._escape('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def test_metrics_are_valid_prometheus_and_admin_only(client: TestClient) -> None:
    client.post("/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN)
    client.put(
        "/api/pools/web",
        json={"image": "w:1", "max_replicas": 2, "desired_replicas": 0},
        headers=ADMIN,
    )
    assert client.get("/api/metrics").status_code == 401
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    worker = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()
    assert (
        client.get(
            "/api/metrics", headers={"Authorization": f"Bearer {worker['token']}"}
        ).status_code
        == 403
    )

    res = client.get("/api/metrics", headers=ADMIN)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain; version=0.0.4")
    families = {
        f.name: f for f in text_string_to_metric_families(res.text)
    }  # must parse
    samples = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for f in families.values()
        for s in f.samples
    }
    assert samples[("argus_tasks", (("state", "ready"),))] == 1
    assert samples[("argus_tasks", (("state", "failed"),))] == 0
    assert samples[("argus_agents", (("state", "active"),))] == 1
    assert samples[("argus_pool_replicas", (("kind", "max"), ("pool", "web")))] == 2
    assert samples[("argus_pool_status", (("pool", "web"), ("status", "ok")))] == 1
    assert samples[("argus_secret_store_up", ())] in (0, 1)
    assert ("argus_build_info", (("version", ops.VERSION),)) in samples
    assert samples[("argus_events_total", ())] >= 2
    assert samples[("argus_uptime_seconds", ())] >= 0


def test_readyz_is_unauthenticated_and_leaks_nothing(client: TestClient) -> None:
    res = client.get("/readyz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) >= {
        "database",
        "reaper",
        "reconciler",
        "disk",
        "secrets",
    }
    assert ADMIN_TOKEN not in res.text


def test_system_summary_for_any_valid_token(client: TestClient) -> None:
    assert client.get("/api/system").status_code == 401
    res = client.get("/api/system", headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["readiness"]["status"] == "ready"
    assert set(body["loops"]) == {"reaper", "reconciler"}
    assert body["tasks"]["ready"] == 0
    assert body["version"] == ops.VERSION
    assert body["disk_guard"]["claim_admission"] in ("admitted", "refused")
    assert {r["role"] for r in body["disk_guard"]["roots"]} == {
        "data",
        "instances",
        "docker",
    }


def test_unready_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "disk_free", lambda _p: 0)
    res = client.get("/readyz")
    assert res.status_code == 503
    assert res.json()["checks"]["disk"]["status"] == "failing"


def test_readyz_does_not_block_on_a_held_writer(client: TestClient) -> None:
    db: Database = client.app.state.db  # type: ignore[attr-defined]
    portal = client.portal  # type: ignore[attr-defined]
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with db.write():
            entered.set()
            await release.wait()

    async def start() -> asyncio.Task[None]:
        task = asyncio.create_task(hold())
        await entered.wait()
        return task

    async def finish(task: asyncio.Task[None]) -> None:
        release.set()
        await task

    holder = portal.call(start)
    try:
        start_t = time.monotonic()
        for _ in range(20):
            assert client.get("/readyz").status_code == 200
        assert time.monotonic() - start_t < ops.WRITE_PROBE_TIMEOUT_S / 2
    finally:
        portal.call(finish, holder)
