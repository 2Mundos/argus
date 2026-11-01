"""Disk guard admission (operations.spec.md 3.5) — synthetic fixtures only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from api_support import insert_agent
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.disk_guard import (
    WARN_EVENT,
    DiskGuard,
    DockerRootQuery,
    RootSample,
    StatFn,
    StatvfsFn,
    Thresholds,
    advance,
    default_docker_root,
    group_by_device,
    measure_roots,
    worst_free,
)
from argus_api.errors import ConflictError
from argus_api.main import app
from argus_api.ops import LoopStats
from argus_api.tasks import service as tasks
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

GiB = 1024 * 1024 * 1024
WARN, CLEAR, REFUSE, RESUME = 5 * GiB, 6 * GiB, 2 * GiB, int(2.5 * GiB)
THRESHOLDS = Thresholds(WARN, CLEAR, REFUSE, RESUME, interval_s=5.0)

ADMIN_TOKEN = "disk-guard-admin-" + "a" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
A = "disk-agent-a"


@dataclass
class FakeStat:
    st_dev: int


@dataclass
class FakeVfs:
    f_bavail: int
    f_frsize: int = 1


def as_stat(fn: Callable[[Path], FakeStat]) -> StatFn:
    return cast(StatFn, fn)


def as_statvfs(fn: Callable[[Path], FakeVfs]) -> StatvfsFn:
    return cast(StatvfsFn, fn)


def _statvfs_boom(_p: Path) -> FakeVfs:
    raise OSError("boom")


def settings_for(tmp_path: Path, **kw) -> Settings:
    base = {
        "data_dir": tmp_path,
        "disk_guard_enabled": True,
        "disk_guard_warn_bytes": WARN,
        "disk_guard_warn_clear_bytes": CLEAR,
        "disk_guard_refuse_bytes": REFUSE,
        "disk_guard_resume_bytes": RESUME,
        "disk_guard_interval_s": 5.0,
    }
    base.update(kw)
    return Settings(**base)


def guard_for(
    db: Database,
    tmp_path: Path,
    *,
    frees: dict[str, int | None] | None = None,
    devs: dict[str, int] | None = None,
    docker: DockerRootQuery | None = None,
    clock: list[float] | None = None,
    instances: Path | None = None,
) -> DiskGuard:
    """Build a guard with injectable stat/statvfs/docker/clock."""
    frees = frees or {}
    devs = devs or {}
    mono = clock if clock is not None else [1000.0]
    s = settings_for(
        tmp_path,
        nanoclaw_instances_root=str(instances) if instances else None,
    )
    g = DiskGuard(s, db, LoopStats("disk_guard", 5.0))
    path_key = {
        tmp_path.resolve(): "data",
        **({instances.resolve(): "instances"} if instances else {}),
    }

    def stat(path: Path) -> FakeStat:
        role = path_key.get(Path(path).resolve(), "other")
        if role in frees and frees[role] is None:
            raise OSError("stat failed")
        return FakeStat(devs.get(role, 1))

    def statvfs(path: Path) -> FakeVfs:
        role = path_key.get(Path(path).resolve(), "other")
        free = frees.get(role, 100 * GiB)
        if free is None:
            raise OSError("statvfs failed")
        return FakeVfs(free)

    if docker is not None:
        docker_path = docker.path
        if docker_path is not None:
            path_key[docker_path.resolve()] = "docker"

        def docker_fn() -> DockerRootQuery:
            return docker

        g._docker_root = docker_fn
    else:
        g._docker_root = lambda: DockerRootQuery("not_applicable")

    g._stat = as_stat(stat)
    g._statvfs = as_statvfs(statvfs)
    g._clock = lambda: mono[0]
    return g


# -- pure measurement / hysteresis ------------------------------------------------


def test_st_dev_dedup_and_worst_band(tmp_path: Path) -> None:
    """Same st_dev → one group with min free; another device is separate; min wins."""
    data = tmp_path / "data"
    instances = tmp_path / "instances"
    docker = tmp_path / "docker"
    data.mkdir()
    instances.mkdir()
    docker.mkdir()
    path_key = {
        data.resolve(): "data",
        instances.resolve(): "instances",
        docker.resolve(): "docker",
    }
    # data + instances share st_dev 1 with DIFFERENT frees (3 GiB / 1 GiB);
    # docker is a second device at 4 GiB. Conservative min on the shared device
    # is 1 GiB, which is also the overall worst.
    frees = {"data": 3 * GiB, "instances": 1 * GiB, "docker": 4 * GiB}
    devs = {"data": 1, "instances": 1, "docker": 2}

    def stat(path: Path) -> FakeStat:
        role = path_key[Path(path).resolve()]
        return FakeStat(devs[role])

    def statvfs(path: Path) -> FakeVfs:
        role = path_key[Path(path).resolve()]
        return FakeVfs(frees[role])

    samples = measure_roots(
        data_dir=data,
        instances_root=instances,
        docker_root=DockerRootQuery("path", docker),
        stat=as_stat(stat),
        statvfs=as_statvfs(statvfs),
    )
    groups = group_by_device(samples)
    measured = [g for g in groups if not g.unknown]
    assert len(measured) == 2
    by_dev = {g.st_dev: g.free_bytes for g in measured}
    assert by_dev[1] == 1 * GiB
    assert by_dev[2] == 4 * GiB
    assert worst_free(groups) == ("measured", 1 * GiB)


def test_unknown_beats_measured_zero() -> None:
    from argus_api.disk_guard import _Group

    measured_zero = [_Group(1, 0)]
    assert worst_free(measured_zero) == ("measured", 0)
    with_unknown = [
        *measured_zero,
        _Group(-1, 0, unknown=True),
    ]
    assert worst_free(with_unknown) == ("unknown", None)
    samples = (
        RootSample("data", "measured", 0, 1),
        RootSample("docker", "unknown"),
    )
    assert worst_free(group_by_device(samples)) == ("unknown", None)


def test_docker_remote_and_unconfigured_are_not_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    assert default_docker_root().kind == "not_applicable"
    monkeypatch.delenv("DOCKER_HOST")
    missing = tmp_path / "no-such.sock"
    assert default_docker_root(socket_path=str(missing)).kind == "not_applicable"


def test_unstatable_docker_root_is_not_applicable(tmp_path: Path) -> None:
    """DockerRootDir the API cannot stat (container case) is not applicable."""
    docker_root = tmp_path / "elsewhere"
    samples = measure_roots(
        data_dir=tmp_path,
        instances_root=None,
        docker_root=DockerRootQuery("path", docker_root),
        stat=as_stat(
            lambda p: FakeStat(1) if Path(p) == tmp_path else (_raise_oserror())
        ),
        statvfs=as_statvfs(lambda p: FakeVfs(50 * GiB)),
    )
    docker = next(s for s in samples if s.role == "docker")
    assert docker.applicability == "not_applicable"
    assert worst_free(group_by_device(samples)) == ("measured", 50 * GiB)


def test_docker_statvfs_failure_stays_unknown(tmp_path: Path) -> None:
    """stat-able DockerRootDir with failed statvfs must refuse, not downgrade."""
    docker_root = tmp_path / "docker-data"
    docker_root.mkdir()

    def stat(path: Path) -> FakeStat:
        return FakeStat(1 if Path(path) == tmp_path else 2)

    def statvfs(path: Path) -> FakeVfs:
        if Path(path) == docker_root:
            raise OSError("statvfs failed")
        return FakeVfs(50 * GiB)

    samples = measure_roots(
        data_dir=tmp_path,
        instances_root=None,
        docker_root=DockerRootQuery("path", docker_root),
        stat=as_stat(stat),
        statvfs=as_statvfs(statvfs),
    )
    docker = next(s for s in samples if s.role == "docker")
    assert docker.applicability == "unknown"
    assert docker.probe_error == "statvfs"
    assert worst_free(group_by_device(samples)) == ("unknown", None)


def test_missing_instances_root_is_absent_not_failed(tmp_path: Path) -> None:
    """An instances_root that does not exist yet holds nothing: absent, not failed."""
    missing = tmp_path / "no-instances"
    samples = measure_roots(
        data_dir=tmp_path,
        instances_root=missing,
        docker_root=DockerRootQuery("not_applicable"),
    )
    assert next(s for s in samples if s.role == "instances").applicability == (
        "not_applicable"
    )


def _raise_oserror() -> FakeStat:
    raise FileNotFoundError("no such path")


def test_hysteresis_boundaries_and_one_warning_per_episode() -> None:
    from argus_api.disk_guard import RootSample, _Machine

    def sample(free: int) -> tuple[RootSample, ...]:
        return (RootSample("data", "measured", free, 1),)

    m = _Machine()
    # Equality at warn does not enter.
    m = advance(m, sample(WARN), THRESHOLDS, now=1.0)
    assert not m.warn_episode and not m.warn_just_entered
    # Strict below warn enters once.
    m = advance(m, sample(WARN - 1), THRESHOLDS, now=2.0)
    assert m.warn_episode and m.warn_just_entered
    m.warn_just_entered = False
    m = advance(m, sample(WARN - 100), THRESHOLDS, now=3.0)
    assert m.warn_episode and not m.warn_just_entered
    # Equality at clear does not exit.
    m = advance(m, sample(CLEAR), THRESHOLDS, now=4.0)
    assert m.warn_episode
    m = advance(m, sample(CLEAR + 1), THRESHOLDS, now=5.0)
    assert not m.warn_episode

    # Refuse / resume boundaries.
    m = advance(m, sample(REFUSE), THRESHOLDS, now=6.0)
    assert not m.refused  # equality does not refuse
    m = advance(m, sample(REFUSE - 1), THRESHOLDS, now=7.0)
    assert m.refused
    m = advance(m, sample(RESUME), THRESHOLDS, now=8.0)
    assert m.refused  # equality does not readmit
    m = advance(m, sample(RESUME + 1), THRESHOLDS, now=9.0)
    assert not m.refused


def test_invalid_config_ordering_names_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", "/tmp")
    monkeypatch.setenv("ARGUS_DISK_GUARD_REFUSE_BYTES", str(REFUSE))
    monkeypatch.setenv("ARGUS_DISK_GUARD_RESUME_BYTES", str(RESUME))
    monkeypatch.setenv("ARGUS_DISK_GUARD_WARN_BYTES", str(WARN))
    monkeypatch.setenv("ARGUS_DISK_GUARD_WARN_CLEAR_BYTES", str(WARN))  # not >
    with pytest.raises(ValueError, match="ARGUS_DISK_GUARD_WARN_CLEAR_BYTES"):
        Settings.from_env()
    monkeypatch.setenv("ARGUS_DISK_GUARD_WARN_CLEAR_BYTES", str(CLEAR))
    monkeypatch.setenv("ARGUS_DISK_GUARD_RESUME_BYTES", str(WARN + GiB))  # > warn
    with pytest.raises(ValueError, match="ARGUS_DISK_GUARD_WARN_BYTES"):
        Settings.from_env()
    monkeypatch.setenv("ARGUS_DISK_GUARD_RESUME_BYTES", str(RESUME))
    monkeypatch.setenv("ARGUS_DISK_GUARD_INTERVAL_S", "0")
    with pytest.raises(ValueError, match="ARGUS_DISK_GUARD_INTERVAL_S"):
        Settings.from_env()


def test_disk_guard_enabled_boolean_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", "/tmp")
    for off in ("false", "FALSE", "0", "no", "OFF"):
        monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", off)
        assert Settings.from_env().disk_guard_enabled is False
    for on in ("true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", on)
        assert Settings.from_env().disk_guard_enabled is True
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "maybe")
    with pytest.raises(ValueError, match="ARGUS_DISK_GUARD_ENABLED"):
        Settings.from_env()


def test_disk_guard_enabled_empty_means_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace ARGUS_DISK_GUARD_ENABLED means the default (true), not error."""
    monkeypatch.setenv("ARGUS_DATA_DIR", "/tmp")
    for empty in ("", " ", "\t"):
        monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", empty)
        assert Settings.from_env().disk_guard_enabled is True


def test_stale_readmission_requires_resume_edge() -> None:
    """After a stale gap, a mid dead-band free must not re-admit."""
    from argus_api.disk_guard import _Machine

    def sample(free: int) -> tuple[RootSample, ...]:
        return (RootSample("data", "measured", free, 1),)

    m = _Machine()
    m = advance(m, sample(10 * GiB), THRESHOLDS, now=1000.0)
    assert not m.refused
    # Snapshot-style stale window elapsed; machine.refused still False.
    mid_band = int(2.2 * GiB)  # refuse < mid < resume
    assert REFUSE < mid_band < RESUME
    m = advance(
        m,
        sample(mid_band),
        THRESHOLDS,
        now=1000.0 + THRESHOLDS.stale_after_s + 1,
    )
    assert m.measurement == "measured"
    assert m.refused
    m = advance(
        m, sample(RESUME + 1), THRESHOLDS, now=1000.0 + THRESHOLDS.stale_after_s + 2
    )
    assert not m.refused


# -- guard service ----------------------------------------------------------------


async def test_statvfs_failure_fails_closed(db: Database, tmp_path: Path) -> None:
    g = guard_for(db, tmp_path, frees={"data": None})
    snap = await g.measure_once()
    assert snap.measurement == "unknown"
    assert snap.free_bytes is None
    assert snap.claim_admission == "refused"
    with pytest.raises(ConflictError) as err:
        g.require_admission()
    assert err.value.code == "disk_low"


async def test_stale_refusal(db: Database, tmp_path: Path) -> None:
    clock = [1000.0]
    g = guard_for(db, tmp_path, frees={"data": 10 * GiB}, clock=clock)
    await g.measure_once()
    assert g.snapshot().claim_admission == "admitted"
    clock[0] = 1000.0 + THRESHOLDS.stale_after_s + 1
    snap = g.snapshot()
    assert snap.measurement == "stale"
    assert snap.claim_admission == "refused"


async def test_one_warn_event_per_episode(db: Database, tmp_path: Path) -> None:
    clock = [1.0]
    g = guard_for(db, tmp_path, frees={"data": WARN - 1}, clock=clock)
    await g.measure_once()
    await g.measure_once()
    async with (
        db.read() as conn,
        conn.execute("SELECT kind FROM events WHERE kind = ?", (WARN_EVENT,)) as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 1
    # Clear and re-enter → second episode, second event.
    g2 = guard_for(db, tmp_path, frees={"data": CLEAR + 1}, clock=clock)
    g2._machine = g._machine
    await g2.measure_once()
    g3 = guard_for(db, tmp_path, frees={"data": WARN - 1}, clock=clock)
    g3._machine = g2._machine
    await g3.measure_once()
    async with (
        db.read() as conn,
        conn.execute("SELECT kind FROM events WHERE kind = ?", (WARN_EVENT,)) as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 2


def _flaky_write_warn(
    g: DiskGuard, db: Database, calls: dict[str, int], *, fail_first: int = 1
):
    """Injectable warn-event writer: fail the first `fail_first` calls, then persist."""
    from argus_api import events as events_mod

    async def write(payload: dict) -> None:
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise RuntimeError("write failed")
        async with db.write() as tx:
            await events_mod.record(tx, "system", "system", "disk", WARN_EVENT, payload)

    g._write_warn = write
    return write


async def test_warn_event_survives_failed_then_succeeding_write(
    db: Database, tmp_path: Path
) -> None:
    """Clear warn_just_entered only after record succeeds; retry must not duplicate.

    Event-write failure must not raise out of measure_once (sampling already advanced).
    """
    clock = [1.0]
    g = guard_for(db, tmp_path, frees={"data": WARN - 1}, clock=clock)
    calls = {"n": 0}
    _flaky_write_warn(g, db, calls)
    snap = await g.measure_once()
    assert snap.measurement == "measured"
    assert g._machine.warn_just_entered
    await g.measure_once()
    assert not g._machine.warn_just_entered
    async with (
        db.read() as conn,
        conn.execute("SELECT kind FROM events WHERE kind = ?", (WARN_EVENT,)) as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert calls["n"] == 2


async def test_warn_event_survives_failed_write_then_episode_clear(
    db: Database, tmp_path: Path
) -> None:
    """Pending warn survives episode clear; a later successful pass records exactly one."""
    clock = [1.0]
    frees: dict[str, int | None] = {"data": WARN - 1}
    g = guard_for(db, tmp_path, frees=frees, clock=clock)
    calls = {"n": 0}
    _flaky_write_warn(g, db, calls)
    await g.measure_once()
    assert g._machine.warn_just_entered
    assert g._machine.warn_episode
    # Episode clears before the retry succeeds; pending flag must still land one event.
    frees["data"] = CLEAR + 1
    clock[0] = 2.0
    await g.measure_once()
    assert not g._machine.warn_episode
    assert not g._machine.warn_just_entered
    async with (
        db.read() as conn,
        conn.execute("SELECT kind FROM events WHERE kind = ?", (WARN_EVENT,)) as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert calls["n"] == 2


async def test_warn_event_retry_records_entry_snapshot(
    db: Database, tmp_path: Path
) -> None:
    """A retried warn event records the entry-pass free/admission, not the retry pass."""
    clock = [1.0]
    entry_free = WARN - 1
    retry_free = WARN - 100
    frees: dict[str, int | None] = {"data": entry_free}
    g = guard_for(db, tmp_path, frees=frees, clock=clock)
    calls = {"n": 0}
    _flaky_write_warn(g, db, calls)
    await g.measure_once()
    frees["data"] = retry_free
    clock[0] = 2.0
    await g.measure_once()
    async with (
        db.read() as conn,
        conn.execute("SELECT data FROM events WHERE kind = ?", (WARN_EVENT,)) as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 1
    data = json.loads(rows[0]["data"])
    assert data["free_bytes"] == entry_free
    assert data["free_bytes"] != retry_free
    assert data["measurement"] == "measured"
    assert data["claim_admission"] == "admitted"


async def test_concurrent_warn_entry_writes_once(db: Database, tmp_path: Path) -> None:
    """Two concurrent measure_once calls on warn entry produce exactly one event."""
    clock = [1.0]
    g = guard_for(db, tmp_path, frees={"data": WARN - 1}, clock=clock)
    await asyncio.gather(g.measure_once(), g.measure_once())
    async with (
        db.read() as conn,
        conn.execute("SELECT kind FROM events WHERE kind = ?", (WARN_EVENT,)) as cur,
    ):
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert not g._machine.warn_just_entered


async def test_all_unknown_samples_instances_applicability(
    db: Database, tmp_path: Path
) -> None:
    """Fail-closed synthetic samples: instances not_applicable when unset, else unknown."""
    g_unset = guard_for(db, tmp_path)
    samples_unset = g_unset._all_unknown_samples()
    assert next(s for s in samples_unset if s.role == "instances").applicability == (
        "not_applicable"
    )
    instances = tmp_path / "instances"
    instances.mkdir()
    g_set = guard_for(db, tmp_path, instances=instances)
    samples_set = g_set._all_unknown_samples()
    assert next(s for s in samples_set if s.role == "instances").applicability == (
        "unknown"
    )


async def test_claim_refused_disk_low_no_attempt(db: Database, tmp_path: Path) -> None:
    await insert_agent(db, A)
    async with db.write() as tx:
        task = await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "t")
    g = guard_for(db, tmp_path, frees={"data": REFUSE - 1})
    await g.measure_once()
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await tasks.claim(tx, A, [], disk_guard=g)
    assert err.value.code == "disk_low"
    async with db.read() as conn:
        got = await tasks.get_task(conn, task.id)
    assert got.state.value == "ready"
    assert got.attempt == 0


async def test_concurrent_claims_under_low_disk(db: Database, tmp_path: Path) -> None:
    await insert_agent(db, A)
    await insert_agent(db, "disk-agent-b")
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "t")
    g = guard_for(db, tmp_path, frees={"data": REFUSE - 1})
    await g.measure_once()

    async def try_claim(agent: str) -> str:
        try:
            async with db.write() as tx:
                await tasks.claim(tx, agent, [], disk_guard=g)
        except ConflictError as exc:
            return exc.code
        return "ok"

    codes = await asyncio.gather(try_claim(A), try_claim("disk-agent-b"))
    assert codes == ["disk_low", "disk_low"]
    async with (
        db.read() as conn,
        conn.execute("SELECT attempt, state FROM tasks") as cur,
    ):
        row = await cur.fetchone()
    assert row["attempt"] == 0 and row["state"] == "ready"


async def test_loop_cancellation(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = guard_for(db, tmp_path, frees={"data": 10 * GiB})
    g.thresholds = Thresholds(WARN, CLEAR, REFUSE, RESUME, interval_s=0.05)
    first_pass = asyncio.Event()
    run_pass = g.run_pass

    async def run_pass_and_signal() -> None:
        await run_pass()
        # The loop records stats right after this returns, before the waiter wakes.
        first_pass.set()

    monkeypatch.setattr(g, "run_pass", run_pass_and_signal)
    task = asyncio.create_task(g.run())
    await first_pass.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert g.stats.passes_total >= 1


# -- HTTP surfaces ----------------------------------------------------------------


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    # Requested so it is set up before this fixture: the autouse fixture in
    # conftest disables the guard, and this one must win for these tests.
    _disk_guard_off: None,
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "true")
    # One startup pass, then no background pass: a test that sets guard state
    # must not race the loop's own measurement.
    monkeypatch.setenv("ARGUS_DISK_GUARD_INTERVAL_S", "3600")
    with TestClient(app) as c:
        yield c


def _worker(client: TestClient) -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()
    return {"Authorization": f"Bearer {reg['token']}"}


def test_claim_http_returns_disk_low(client: TestClient) -> None:
    client.post("/api/tasks", json={"project": "demo", "title": "t"}, headers=ADMIN)
    worker = _worker(client)
    guard: DiskGuard = client.app.state.disk_guard
    guard._machine.refused = True
    guard._machine.last_success_mono = guard._clock()
    guard._machine.measurement = "measured"
    guard._machine.free_bytes = REFUSE - 1
    res = client.post(
        "/api/tasks/claim", json={"capabilities": [], "wait_s": 0}, headers=worker
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "disk_low"


def test_chat_claim_returns_disk_low(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus_api.conversations import routes as chat_routes

    async def system_pool(_conn, _agent_id: str) -> str:
        return "orchestrator"

    monkeypatch.setattr(chat_routes, "agent_system_pool", system_pool)
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "orchestrator"}, headers=ADMIN
    ).json()
    reg = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()
    orch = {"Authorization": f"Bearer {reg['token']}"}
    guard: DiskGuard = client.app.state.disk_guard
    guard._machine.refused = True
    guard._machine.last_success_mono = guard._clock()
    res = client.get("/api/agents/me/chat", headers=orch)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "disk_low"


def test_readyz_contract_unchanged_and_no_disk_guard_loop(client: TestClient) -> None:
    res = client.get("/readyz")
    assert res.status_code == 200
    checks = set(res.json()["checks"])
    assert checks == {"database", "reaper", "reconciler", "disk", "secrets"}
    body = client.get("/api/system", headers=ADMIN).json()
    assert set(body["loops"]) == {"reaper", "reconciler"}
    assert "disk_guard" not in body["loops"]


def _assert_disk_guard_redacted(dg: dict, tmp_path: Path) -> None:
    """Same redaction guarantees for admin and non-admin callers."""
    text = str(dg)
    assert str(tmp_path) not in text
    assert "st_dev" not in text
    assert "/var/lib/docker" not in text
    assert "DockerRootDir" not in text
    assert "docker.sock" not in text
    assert dg["claim_admission"] in ("admitted", "refused")
    assert {r["role"] for r in dg["roots"]} == {"data", "instances", "docker"}
    for r in dg["roots"]:
        assert set(r) <= {"role", "applicability", "free_bytes", "band"}
        assert "path" not in r
        assert "st_dev" not in r


def test_system_disk_guard_redaction_and_refresh(
    client: TestClient, tmp_path: Path
) -> None:
    body = client.get("/api/system", headers=ADMIN).json()
    dg = body["disk_guard"]
    _assert_disk_guard_redacted(dg, tmp_path)

    guard: DiskGuard = client.app.state.disk_guard
    assert guard.enabled
    before = dg["claim_admission"]
    guard._machine.refused = True
    guard._machine.measurement = "measured"
    guard._machine.free_bytes = REFUSE - 1
    guard._machine.last_success_mono = guard._clock()
    guard._machine.roots = ()
    after = client.get("/api/system", headers=ADMIN).json()["disk_guard"]
    assert after["claim_admission"] == "refused"
    assert after["claim_admission"] != before or before == "refused"


def test_system_disk_guard_redaction_for_worker(
    client: TestClient, tmp_path: Path
) -> None:
    """Non-admin valid token can read /api/system; disk_guard stays path-free."""
    worker = _worker(client)
    res = client.get("/api/system", headers=worker)
    assert res.status_code == 200
    _assert_disk_guard_redacted(res.json()["disk_guard"], tmp_path)


def test_startup_survives_warn_event_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _disk_guard_off: None,
) -> None:
    """Warn-event INSERT failure must not abort a healthy boot; retry records once."""
    from argus_api import events as events_mod

    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "true")
    monkeypatch.setenv("ARGUS_DISK_GUARD_INTERVAL_S", "3600")
    monkeypatch.setenv("ARGUS_DISK_GUARD_WARN_BYTES", str(WARN))
    monkeypatch.setenv("ARGUS_DISK_GUARD_WARN_CLEAR_BYTES", str(CLEAR))
    monkeypatch.setenv("ARGUS_DISK_GUARD_REFUSE_BYTES", str(REFUSE))
    monkeypatch.setenv("ARGUS_DISK_GUARD_RESUME_BYTES", str(RESUME))

    entry_free = WARN - 1
    frees: dict[str, int] = {"data": entry_free}
    write_calls = {"n": 0}
    orig_post_init = DiskGuard.__post_init__

    def patched_post_init(self: DiskGuard) -> None:
        orig_post_init(self)
        if not self.enabled:
            return
        self._stat = as_stat(lambda _p: FakeStat(1))
        self._statvfs = as_statvfs(lambda _p: FakeVfs(frees["data"]))
        self._docker_root = lambda: DockerRootQuery("not_applicable")
        self._clock = lambda: 1.0

        async def flaky(payload: dict) -> None:
            write_calls["n"] += 1
            if write_calls["n"] == 1:
                raise RuntimeError("write failed")
            async with self.db.write() as tx:
                await events_mod.record(
                    tx, "system", "system", "disk", WARN_EVENT, payload
                )

        self._write_warn = flaky

    monkeypatch.setattr(DiskGuard, "__post_init__", patched_post_init)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/system", headers=ADMIN).status_code == 200
        guard: DiskGuard = client.app.state.disk_guard
        assert guard.enabled
        assert guard._machine.warn_episode
        assert guard._machine.warn_just_entered
        assert guard._machine.warn_entry_free_bytes == entry_free
        assert write_calls["n"] == 1

        # Free has left the warn band; the pending entry snapshot must still record.
        frees["data"] = CLEAR + 1
        assert client.portal is not None
        client.portal.call(guard.measure_once)
        assert not guard._machine.warn_just_entered
        assert not guard._machine.warn_episode
        assert write_calls["n"] == 2

        db: Database = client.app.state.db
        rows = client.portal.call(_warn_event_rows, db)
        assert len(rows) == 1
        data = json.loads(rows[0]["data"])
        assert data["free_bytes"] == entry_free
        assert data["measurement"] == "measured"


async def _warn_event_rows(db: Database) -> list:
    async with (
        db.read() as conn,
        conn.execute("SELECT data FROM events WHERE kind = ?", (WARN_EVENT,)) as cur,
    ):
        return list(await cur.fetchall())


def test_mcp_claim_task_surfaces_disk_low_via_http(client: TestClient) -> None:
    """MCP claim_task POSTs /tasks/claim; refusal is the same typed error."""
    client.post("/api/tasks", json={"project": "demo", "title": "t"}, headers=ADMIN)
    worker = _worker(client)
    guard: DiskGuard = client.app.state.disk_guard
    guard._machine.refused = True
    guard._machine.last_success_mono = guard._clock()
    # Direct HTTP is what the MCP tool calls.
    res = client.post(
        "/api/tasks/claim",
        json={"capabilities": [], "wait_s": 0},
        headers=worker,
    )
    assert res.json()["detail"]["code"] == "disk_low"


async def test_accounts_claim_uses_shared_guard(db: Database, tmp_path: Path) -> None:
    from argus_api.accounts import service as accounts
    from argus_api.secrets import MemorySecretStore

    await insert_agent(db, A)
    async with db.write() as tx:
        await tasks.create_task(tx, TaskCreate(project="demo", title="t"), "t")
    g = guard_for(db, tmp_path, frees={"data": REFUSE - 1})
    await g.measure_once()
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await accounts.claim(tx, A, [], None, MemorySecretStore(), disk_guard=g)
    assert err.value.code == "disk_low"


async def test_failure_then_recovery_async(db: Database, tmp_path: Path) -> None:
    clock = [10.0]
    g = guard_for(db, tmp_path, frees={"data": 10 * GiB}, clock=clock)
    await g.measure_once()
    assert g.snapshot().claim_admission == "admitted"
    g._statvfs = as_statvfs(_statvfs_boom)
    snap = await g.measure_once()
    assert snap.measurement == "unknown" and snap.claim_admission == "refused"
    g._statvfs = as_statvfs(lambda _p: FakeVfs(RESUME + 1))
    g._stat = as_stat(lambda _p: FakeStat(1))
    clock[0] = 20.0
    snap = await g.measure_once()
    assert snap.claim_admission == "admitted"


async def test_non_oserror_measurement_fails_closed(
    db: Database, tmp_path: Path
) -> None:
    clock = [10.0]
    g = guard_for(db, tmp_path, frees={"data": 10 * GiB}, clock=clock)
    await g.measure_once()
    assert g.snapshot().claim_admission == "admitted"

    def boom() -> DockerRootQuery:
        raise RuntimeError("unexpected")

    g._docker_root = boom
    snap = await g.measure_once()
    assert snap.measurement == "unknown"
    assert snap.claim_admission == "refused"
    assert snap.free_bytes is None


async def test_configured_docker_socket_passed_to_resolver(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "custom.sock"
    sock.write_text("")
    seen: list[str] = []

    def capture(*, docker_host=None, socket_path="/var/run/docker.sock"):
        seen.append(socket_path)
        return DockerRootQuery("not_applicable")

    monkeypatch.setattr(
        "argus_api.disk_guard.default_docker_root", capture, raising=True
    )
    s = settings_for(tmp_path, nanoclaw_docker_socket=str(sock))
    # Rebuild guard so __post_init__ binds the (patched) default resolver.
    from argus_api.disk_guard import DiskGuard as DG

    g = DG(s, db, LoopStats("disk_guard", 5.0))
    g._stat = as_stat(lambda _p: FakeStat(1))
    g._statvfs = as_statvfs(lambda _p: FakeVfs(10 * GiB))
    g._clock = lambda: 1.0
    await g.measure_once()
    assert seen == [str(sock)]


async def test_disabled_snapshot_is_not_measured(db: Database, tmp_path: Path) -> None:
    s = settings_for(tmp_path, disk_guard_enabled=False)
    g = DiskGuard(s, db, LoopStats("disk_guard", 5.0))
    snap = g.snapshot()
    assert snap.enabled is False
    assert snap.measurement == "disabled"
    assert snap.free_bytes is None
    assert snap.claim_admission == "admitted"
