"""Disk guard admission (operations.spec.md 3.5) — synthetic fixtures only."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from api_support import insert_agent
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.disk_guard import (
    WARN_EVENT,
    DiskGuard,
    DockerRootQuery,
    RootSample,
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

    g._stat = stat  # type: ignore[method-assign]
    g._statvfs = statvfs  # type: ignore[method-assign]
    g._clock = lambda: mono[0]
    return g


# -- pure measurement / hysteresis ------------------------------------------------


def test_st_dev_dedup_and_worst_band(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    samples = measure_roots(
        data_dir=a,
        instances_root=b,
        docker_root=DockerRootQuery("not_applicable"),
        stat=lambda p: FakeStat(1 if "a" in str(p) or "b" in str(p) else 2),  # type: ignore[arg-type]
        statvfs=lambda p: FakeVfs(3 * GiB if "a" in str(p) else 1 * GiB),  # type: ignore[arg-type]
    )
    groups = group_by_device(samples)
    # Same st_dev → one group; free is from either path (same device).
    assert len([g for g in groups if not g.unknown]) == 1
    assert "data" in next(g.roles for g in groups if not g.unknown)


def test_unknown_beats_measured_zero() -> None:
    from argus_api.disk_guard import _Group

    measured_zero = [_Group(1, 0, frozenset({"data"}))]
    assert worst_free(measured_zero) == ("measured", 0)
    with_unknown = [
        *measured_zero,
        _Group(-1, 0, frozenset({"docker"}), unknown=True),
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
        stat=lambda p: FakeStat(1) if Path(p) == tmp_path else (_raise_oserror()),  # type: ignore[arg-type]
        statvfs=lambda p: FakeVfs(50 * GiB),  # type: ignore[arg-type]
    )
    docker = next(s for s in samples if s.role == "docker")
    assert docker.applicability == "not_applicable"
    assert worst_free(group_by_device(samples)) == ("measured", 50 * GiB)


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


async def test_claim_refused_disk_low_no_attempt(db: Database, tmp_path: Path) -> None:
    await insert_agent(db, A)
    async with db.write() as tx:
        task = await tasks.create_task(tx, TaskCreate(project="global", title="t"), "t")
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
        await tasks.create_task(tx, TaskCreate(project="global", title="t"), "t")
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


async def test_loop_cancellation(db: Database, tmp_path: Path) -> None:
    g = guard_for(db, tmp_path, frees={"data": 10 * GiB})
    g.thresholds = Thresholds(WARN, CLEAR, REFUSE, RESUME, interval_s=0.05)
    task = asyncio.create_task(g.run())
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert g.stats.passes_total >= 1


# -- HTTP surfaces ----------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
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
    client.post("/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN)
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


def test_system_disk_guard_redaction_and_refresh(
    client: TestClient, tmp_path: Path
) -> None:
    body = client.get("/api/system", headers=ADMIN).json()
    dg = body["disk_guard"]
    text = str(dg)
    assert str(tmp_path) not in text
    assert "st_dev" not in text
    assert "/var/lib/docker" not in text
    assert dg["claim_admission"] in ("admitted", "refused")
    assert {r["role"] for r in dg["roots"]} == {"data", "instances", "docker"}
    for r in dg["roots"]:
        assert "path" not in r
        assert "st_dev" not in r

    guard: DiskGuard = client.app.state.disk_guard
    before = dg["claim_admission"]
    guard._machine.refused = True
    guard._machine.measurement = "measured"
    guard._machine.free_bytes = REFUSE - 1
    guard._machine.last_success_mono = guard._clock()
    guard._machine.roots = ()
    after = client.get("/api/system", headers=ADMIN).json()["disk_guard"]
    assert after["claim_admission"] == "refused"
    assert after["claim_admission"] != before or before == "refused"


def test_mcp_claim_task_surfaces_disk_low_via_http(client: TestClient) -> None:
    """MCP claim_task POSTs /tasks/claim; refusal is the same typed error."""
    client.post("/api/tasks", json={"project": "global", "title": "t"}, headers=ADMIN)
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
        await tasks.create_task(tx, TaskCreate(project="global", title="t"), "t")
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
    g._statvfs = lambda _p: (_ for _ in ()).throw(OSError("boom"))  # type: ignore[method-assign]
    snap = await g.measure_once()
    assert snap.measurement == "unknown" and snap.claim_admission == "refused"
    g._statvfs = lambda _p: FakeVfs(RESUME + 1)  # type: ignore[method-assign]
    g._stat = lambda _p: FakeStat(1)  # type: ignore[method-assign]
    clock[0] = 20.0
    snap = await g.measure_once()
    assert snap.claim_admission == "admitted"
