"""Inspect, safe option updates, redaction and recycling (MCP operations 3.3)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import DONE, FakeTarget
from argus_api.agents import service as agents
from argus_api.db import Database
from argus_api.errors import ConflictError
from argus_api.fleet import service as fleet
from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
from argus_api.fleet.targets import TargetRegistry
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.agents import AgentRegister, AgentRuntime
from argus_core.fleet import PoolUpsert
from argus_core.tasks import TaskCreate
from fastapi.testclient import TestClient

SETTINGS = FleetSettings(argus_url="http://argus.test:8000")


@pytest.fixture
def target() -> FakeTarget:
    return FakeTarget()


@pytest.fixture
def registry(target: FakeTarget) -> TargetRegistry:
    r = TargetRegistry()
    r.override("docker", None, target)
    return r


async def make_pool(db: Database, desired: int = 1) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            "web",
            PoolUpsert(
                image="worker:1",
                capabilities=["py"],
                desired_replicas=desired,
                max_replicas=3,
            ),
            "admin:test",
        )


async def tick(db: Database, registry: TargetRegistry) -> None:
    await reconcile_pool(db, "web", registry, SETTINGS)


async def live(db: Database):
    async with db.read() as conn:
        return await fleet.list_instances(conn, "web")


async def register_from(db: Database, target: FakeTarget, name: str) -> str:
    token = target.specs[name].env["ARGUS_BOOTSTRAP_TOKENS"].split(",")[0]
    async with db.write() as tx:
        reg = await agents.register(
            tx, AgentRegister(bootstrap_token=token, capabilities=["py", "git"])
        )
    return reg.agent.id


async def inspect(db: Database):
    async with db.read() as conn:
        return await fleet.inspect(conn, "web")


# -- inspect ----------------------------------------------------------------------


async def test_inspect_says_how_each_fact_is_known(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    await make_pool(db)
    await tick(db, registry)  # reserves and starts
    [inst] = (await inspect(db)).instances
    rt = inst.runtime
    assert (rt.image.value, rt.image.source) == ("worker:1", "requested")
    assert rt.image_digest.source == "unknown"  # not observed running yet
    assert (rt.capabilities.value, rt.capabilities.source) == (["py"], "configured")
    assert (rt.ready.value, rt.ready.source) == (None, "unknown")
    assert rt.worker_version.source == rt.engine.source == "unknown"

    await tick(db, registry)  # observes the running container
    agent_id = await register_from(db, target, inst.provider_ref)
    async with db.write() as tx:
        runtime = AgentRuntime(worker_version="1.4.0", engine="codex")
        await agents.heartbeat(tx, agent_id, [], runtime)
    async with db.write() as tx:  # a heartbeat without runtime keeps the last one
        await agents.heartbeat(tx, agent_id, [])
    [inst] = (await inspect(db)).instances
    rt = inst.runtime
    assert (rt.image_digest.value, rt.image_digest.source) == (
        "sha256:worker:1",
        "started",
    )
    assert (rt.worker_version.value, rt.worker_version.source) == ("1.4.0", "validated")
    assert (rt.engine.value, rt.executor.source) == ("codex", "unknown")
    assert (rt.capabilities.value, rt.capabilities.source) == (
        ["git", "py"],
        "validated",
    )
    assert (rt.ready.value, rt.ready.source) == (True, "validated")


# -- recycle ----------------------------------------------------------------------


async def running_instance_with_agent(db, registry, target):
    await make_pool(db)
    await tick(db, registry)
    await tick(db, registry)
    [inst] = await live(db)
    return inst, await register_from(db, target, inst.provider_ref)


async def test_recycle_waits_for_claimed_work_then_replaces(
    db: Database, registry: TargetRegistry, target: FakeTarget
) -> None:
    inst, agent_id = await running_instance_with_agent(db, registry, target)
    async with db.write() as tx:
        await tasks.create_task(
            tx, TaskCreate(project="global", title="t"), "admin:test"
        )
        task = await tasks.claim(tx, agent_id, [])
    assert task is not None
    async with db.write() as tx:
        await fleet.recycle(tx, inst.id, "admin:test")
        await fleet.recycle(tx, inst.id, "admin:test")  # a retry changes nothing
    for _ in range(3):  # busy: never stopped, never replaced early
        await tick(db, registry)
    [still] = await live(db)
    assert (still.id, still.state, still.draining) == (inst.id, "running", True)
    async with db.read() as conn:
        assert (await agents.get_agent(conn, agent_id)).state == "draining"

    async with db.write() as tx:  # the work finishes undisturbed
        await tasks.complete(tx, agent_id, task.id, task.attempt, DONE)
    await tick(db, registry)  # idle now: stop it
    await tick(db, registry)  # confirmed gone; a replacement is reserved
    [fresh] = await live(db)
    assert fresh.id != inst.id
    async with db.read() as conn:
        old = await fleet.get_instance(conn, inst.id)
    assert (old.state, old.exit_reason) == ("terminated", "drained")


async def test_recycle_only_running_instances(
    db: Database, registry: TargetRegistry
) -> None:
    await make_pool(db)
    await tick(db, registry)
    [inst] = await live(db)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE instances SET state = 'reserved' WHERE id = ?", (inst.id,)
        )
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await fleet.recycle(tx, inst.id, "admin:test")
    assert err.value.code == "instance_not_running"


# -- HTTP: redaction and PATCH ----------------------------------------------------

ADMIN_TOKEN = "test-admin-token-" + "f" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        put = c.put(
            "/api/pools/web",
            json={
                "image": "worker:1",
                "max_replicas": 2,
                "env": {"TOKEN": "s3cret", "MODE": "fast"},
                "capabilities": ["py"],
            },
            headers=ADMIN,
        )
        assert put.status_code == 200
        assert put.json()["env"] == {"TOKEN": "***", "MODE": "***"}
        yield c


def orchestrator(client: TestClient) -> dict[str, str]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "orchestrator"}, headers=ADMIN
    ).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_env_values_never_leave_except_to_an_admin_who_asks(
    client: TestClient,
) -> None:
    orch = orchestrator(client)
    for res in (
        client.get("/api/pools", headers=orch),
        client.get("/api/pools/web", headers=orch),
        client.get("/api/pools/web/inspect", headers=orch),
        client.post("/api/pools/web/scale", json={"desired_replicas": 1}, headers=orch),
    ):
        assert res.status_code == 200, res.text
        assert "s3cret" not in res.text
    denied = client.get("/api/pools/web", params={"reveal_env": True}, headers=orch)
    assert denied.status_code == 403
    shown = client.get("/api/pools/web", params={"reveal_env": True}, headers=ADMIN)
    assert shown.json()["env"] == {"TOKEN": "s3cret", "MODE": "fast"}


def test_patch_keeps_what_it_does_not_name(client: TestClient) -> None:
    pool = client.get("/api/pools/web", headers=ADMIN).json()
    res = client.patch(
        "/api/pools/web",
        json={
            "expected_version": pool["version"],
            "max_replicas": 4,
            "env": {"MODE": "slow", "TOKEN": None, "NEW": "1"},
        },
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    patched = res.json()
    assert (patched["max_replicas"], patched["image"]) == (4, "worker:1")
    assert patched["capabilities"] == ["py"]
    assert patched["version"] != pool["version"]
    shown = client.get("/api/pools/web", params={"reveal_env": True}, headers=ADMIN)
    assert shown.json()["env"] == {"MODE": "slow", "NEW": "1"}

    stale = client.patch(
        "/api/pools/web",
        json={"expected_version": pool["version"], "max_replicas": 5},
        headers=ADMIN,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_version"
    assert stale.json()["detail"]["details"] == [patched["version"]]


def test_patch_refuses_fixed_fields_and_inconsistent_results(
    client: TestClient,
) -> None:
    version = client.get("/api/pools/web", headers=ADMIN).json()["version"]
    for change in ({"target": "kubernetes"}, {"runtime": "nanoclaw"}, {"name": "x"}):
        res = client.patch(
            "/api/pools/web",
            json={"expected_version": version, **change},
            headers=ADMIN,
        )
        assert res.status_code == 422, change
    res = client.patch(
        "/api/pools/web",
        json={"expected_version": version, "desired_replicas": 9},
        headers=ADMIN,
    )
    assert res.json()["detail"]["code"] == "invalid_pool"
    orch = orchestrator(client)
    res = client.patch(
        "/api/pools/web", json={"expected_version": version}, headers=orch
    )
    assert res.status_code == 403
