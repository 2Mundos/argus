"""Worker launch and node capabilities (docs/specs/worker-launch.spec.md)."""

import os
import tempfile
import time
from collections.abc import Generator
from pathlib import Path

import pytest
from api_support import FakeTarget
from argus_api.main import app
from fastapi.testclient import TestClient
from test_mcp import rpc, tool

ADMIN_TOKEN = "test-admin-token-" + "l" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
POOL = {"image": "worker:1", "max_replicas": 2, "capabilities": ["python"]}


@pytest.fixture
def target() -> FakeTarget:
    return FakeTarget()


@pytest.fixture
def public_url() -> str | None:
    return "http://argus.test:8000"


@pytest.fixture
def extra_env() -> dict[str, str]:
    return {}


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: FakeTarget,
    public_url: str | None,
    extra_env: dict[str, str],
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    if public_url:
        monkeypatch.setenv("ARGUS_PUBLIC_URL", public_url)
    else:
        monkeypatch.delenv("ARGUS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("ARGUS_RECONCILE_INTERVAL_S", "0.2")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    for name in list(os.environ):
        if name.startswith(("ARGUS_NANOCLAW_", "ARGUS_WORKER_", "ARGUS_FLEET_")):
            monkeypatch.delenv(name)
    for name, value in extra_env.items():
        monkeypatch.setenv(name, value)
    with TestClient(app) as c:
        # Never the machine's real Docker: every docker target is the fake.
        app.state.targets.override("docker", None, target)
        yield c


def agent_token(client: TestClient, role: str) -> str:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()
    return client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_pool(client: TestClient, name: str = "web", **kw) -> None:
    res = client.put(f"/api/pools/{name}", json={**POOL, **kw}, headers=ADMIN)
    assert res.status_code == 200, res.text


def wait_for(fn, timeout: float = 10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value := fn():
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


# -- permissions --------------------------------------------------------------------


def test_workers_cannot_discover_or_launch(client: TestClient) -> None:
    make_pool(client)
    worker = bearer(agent_token(client, "worker"))
    assert client.get("/api/fleet/capabilities", headers=worker).status_code == 403
    res = client.post("/api/pools/web/launch", json={}, headers=worker)
    assert res.status_code == 403
    assert client.get("/api/pools/web", headers=ADMIN).json()["desired_replicas"] == 0
    assert client.post("/api/pools/web/launch", json={}).status_code == 401


def test_admin_and_orchestrator_can_launch(client: TestClient) -> None:
    make_pool(client, max_replicas=3)
    orch = bearer(agent_token(client, "orchestrator"))
    assert client.get("/api/fleet/capabilities", headers=orch).status_code == 200
    for headers in (ADMIN, orch):
        res = client.post("/api/pools/web/launch", json={}, headers=headers)
        assert res.status_code == 200, res.text
    assert client.get("/api/pools/web", headers=ADMIN).json()["desired_replicas"] == 2


# -- discovery ----------------------------------------------------------------------


def test_discovery_available(client: TestClient, target: FakeTarget) -> None:
    make_pool(client, cpus=2, memory_mb=1024)
    caps = client.get("/api/fleet/capabilities", headers=ADMIN).json()
    assert caps["public_url_configured"] is True
    [node] = caps["targets"]
    assert (node["available"], node["runtimes"], node["cpus"]) == (True, ["runc"], 4)
    [pool] = caps["pools"]
    assert (pool["launchable"], pool["free_slots"], pool["blockers"]) == (True, 2, [])


def test_discovery_unavailable(client: TestClient, target: FakeTarget) -> None:
    make_pool(client)
    make_pool(client, "boxed", docker={"runtime": "sysbox-runc"})
    make_pool(client, "huge", memory_mb=999999)
    target.reachable = False
    caps = client.get("/api/fleet/capabilities", headers=ADMIN).json()
    assert caps["targets"][0]["available"] is False
    assert all(not p["launchable"] for p in caps["pools"])
    target.reachable = True
    by = {
        p["name"]: p
        for p in client.get("/api/fleet/capabilities", headers=ADMIN).json()["pools"]
    }
    assert by["web"]["launchable"]
    assert not by["boxed"]["launchable"]
    assert "sysbox-runc" in by["boxed"]["blockers"][0]
    assert not by["huge"]["launchable"]


def test_discovery_reports_images_and_unverifiable_paths(
    client: TestClient, target: FakeTarget
) -> None:
    target.images = {"worker:1"}
    make_pool(client, docker={"host": "ssh://far"})
    client.app.state.targets.override("docker", "ssh://far", target)
    [pool] = client.get("/api/fleet/capabilities", headers=ADMIN).json()["pools"]
    image = next(c for c in pool["checks"] if c["name"] == "image")
    assert image["ok"] is True
    target.images = set()
    [pool] = client.get("/api/fleet/capabilities", headers=ADMIN).json()["pools"]
    image = next(c for c in pool["checks"] if c["name"] == "image")
    assert image["ok"] is None and pool["launchable"]  # pullable: not a blocker


def test_nanoclaw_local_paths_are_checked(
    client: TestClient, tmp_path: Path, target: FakeTarget
) -> None:
    from dataclasses import replace

    client.app.state.settings = replace(
        client.app.state.settings, nanoclaw_allow_docker_socket=True
    )
    target.shares_filesystem = True
    nc = {  # instances_root is length-limited: keep these short
        "install_dir": str(tmp_path),
        "instances_root": "/nonexistent-argus-test",
        "agent_image": "nc-agent:1",
        "docker_socket": "/nonexistent-argus-test.sock",
    }
    make_pool(client, "nc", runtime="nanoclaw", nanoclaw=nc)
    [pool] = client.get("/api/fleet/capabilities", headers=ADMIN).json()["pools"]
    assert not pool["launchable"]
    assert len(pool["blockers"]) == 2  # instances_root and docker socket
    assert (
        client.post("/api/pools/nc/launch", json={}, headers=ADMIN).status_code == 409
    )


def test_remote_daemon_paths_are_unverified_not_ok(client: TestClient) -> None:
    from dataclasses import replace

    client.app.state.settings = replace(
        client.app.state.settings, nanoclaw_allow_docker_socket=True
    )
    # The paths exist here, but the daemon does not share this filesystem:
    # existing here proves nothing about the daemon's host.
    with tempfile.TemporaryDirectory(
        prefix="a"
    ) as d:  # instances_root is length-limited
        nc = {
            "install_dir": d,
            "instances_root": d,
            "agent_image": "nc-agent:1",
            "docker_socket": d,
        }
        make_pool(client, "nc", runtime="nanoclaw", nanoclaw=nc)
        [pool] = client.get("/api/fleet/capabilities", headers=ADMIN).json()["pools"]
    names = ("nanoclaw_install_dir", "nanoclaw_instances_root", "docker_socket")
    paths = [c for c in pool["checks"] if c["name"] in names]
    assert len(paths) == 3 and all(c["ok"] is None for c in paths)


# -- launching ----------------------------------------------------------------------


def test_launch_starts_and_tracks_an_instance(
    client: TestClient, target: FakeTarget
) -> None:
    make_pool(client)
    res = client.post("/api/pools/web/launch", json={}, headers=ADMIN)
    assert res.status_code == 200
    assert res.json()["requested"] and res.json()["desired_replicas"] == 1
    inst = wait_for(
        lambda: next(
            (
                i
                for i in client.get("/api/instances", headers=ADMIN).json()
                if i["state"] == "running"
            ),
            None,
        )
    )
    assert inst["provider_ref"] in target.containers
    assert len(target.started) == 1


def test_launch_failure_is_visible_not_success(
    client: TestClient, target: FakeTarget
) -> None:
    make_pool(client)
    target.fail_start = True
    assert (
        client.post("/api/pools/web/launch", json={}, headers=ADMIN).status_code == 200
    )
    pool = wait_for(
        lambda: (
            (p := client.get("/api/pools/web", headers=ADMIN).json())["status"]
            == "failing"
            and p
        )
    )
    assert "image not found" in pool["last_error"]
    [inst] = client.get("/api/instances", headers=ADMIN).json()
    assert inst["state"] == "reserved" and "image not found" in inst["last_error"]


def test_launch_refused_changes_nothing(client: TestClient, target: FakeTarget) -> None:
    make_pool(client)
    target.reachable = False
    res = client.post("/api/pools/web/launch", json={}, headers=ADMIN)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "not_launchable"
    assert "unreachable" in res.json()["detail"]["details"][0]
    assert client.get("/api/pools/web", headers=ADMIN).json()["desired_replicas"] == 0
    assert (
        client.post("/api/pools/nope/launch", json={}, headers=ADMIN).status_code == 404
    )


def test_launch_respects_cap_and_is_not_double_submitted(client: TestClient) -> None:
    make_pool(client, max_replicas=1)
    body = {"expected_desired_replicas": 0}
    assert (
        client.post("/api/pools/web/launch", json=body, headers=ADMIN).status_code
        == 200
    )
    again = client.post("/api/pools/web/launch", json=body, headers=ADMIN)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "stale_pool"
    over = client.post("/api/pools/web/launch", json={}, headers=ADMIN)
    assert over.json()["detail"]["code"] == "pool_at_capacity"
    assert client.get("/api/pools/web", headers=ADMIN).json()["desired_replicas"] == 1


@pytest.mark.parametrize("public_url", [None])
def test_no_public_url_blocks_launch(client: TestClient) -> None:
    make_pool(client)
    caps = client.get("/api/fleet/capabilities", headers=ADMIN).json()
    assert caps["public_url_configured"] is False
    res = client.post("/api/pools/web/launch", json={}, headers=ADMIN)
    assert res.status_code == 409
    assert "ARGUS_PUBLIC_URL" in res.json()["detail"]["details"][0]


# -- MCP ----------------------------------------------------------------------------


def test_mcp_discovery_and_launch(client: TestClient) -> None:
    make_pool(client)
    orch = agent_token(client, "orchestrator")
    worker = agent_token(client, "worker")
    names = {t["name"] for t in rpc(client, orch, "tools/list")["tools"]}
    assert {"get_fleet_capabilities", "launch_worker"} <= names
    caps = tool(client, orch, "get_fleet_capabilities")
    assert caps["pools"][0]["launchable"] is True
    got = tool(client, orch, "launch_worker", pool="web", expected_desired_replicas=0)
    assert got["requested"] is True and got["desired_replicas"] == 1
    stale = tool(client, orch, "launch_worker", pool="web", expected_desired_replicas=0)
    assert stale["error"].startswith("409 stale_pool:")
    for name, args in (
        ("launch_worker", {"pool": "web"}),
        ("get_fleet_capabilities", {}),
    ):
        assert tool(client, worker, name, **args)["error"].startswith("403 forbidden")
