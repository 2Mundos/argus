"""NanoClaw socket opt-in, runtime/socket conflict, and container hardening."""

import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from argus_api.fleet.targets import RUNTIME_TMPFS, DockerTarget, Mount
from argus_api.main import app
from argus_core.fleet import DockerOptions, NanoclawOptions, PoolUpsert
from fastapi.testclient import TestClient
from pydantic import ValidationError

ADMIN_TOKEN = "test-admin-token-" + "h" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

SOCKET_POOL = {
    "runtime": "nanoclaw",
    "image": "argus-nanoclaw-host:dev",
    "desired_replicas": 0,
    "max_replicas": 1,
    "nanoclaw": {
        "install_dir": "/opt/nanoclaw",
        "instances_root": "/srv/nc-i",
        "agent_image": "nanoclaw-agent:1",
        "user": "1000:1000",
        "group_add": ["999"],
    },
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.delenv("ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET", raising=False)
    for name in list(os.environ):
        if name.startswith("ARGUS_NANOCLAW_"):
            monkeypatch.delenv(name, raising=False)
    with TestClient(app) as c:
        yield c


def test_put_rejects_socket_pool_without_opt_in(client: TestClient) -> None:
    res = client.put("/api/pools/nc-sock", json=SOCKET_POOL, headers=ADMIN)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "docker_socket_not_allowed"


def test_put_accepts_socket_pool_with_opt_in(client: TestClient) -> None:
    from dataclasses import replace

    # Settings are snapshotted at lifespan start; flip the process policy in place.
    client.app.state.settings = replace(
        client.app.state.settings, nanoclaw_allow_docker_socket=True
    )
    res = client.put("/api/pools/nc-sock", json=SOCKET_POOL, headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["docker"]["runtime"] is None


def test_put_rejects_sandbox_with_docker_socket(client: TestClient) -> None:
    body = {
        "runtime": "nanoclaw",
        "image": "argus-nanoclaw-host:dev",
        "desired_replicas": 0,
        "max_replicas": 1,
        "docker": {"runtime": "sysbox-runc"},
        "nanoclaw": {
            "install_dir": "/opt/nanoclaw",
            "instances_root": "/srv/nc-i",
            "agent_image": "nanoclaw-agent:1",
            "docker_socket": "/var/run/docker.sock",
            "agent_image_archive": "/srv/agent.tar",
        },
    }
    res = client.put("/api/pools/nc-box", json=body, headers=ADMIN)
    assert res.status_code == 422, res.text


def test_sandboxed_pool_upsert_without_opt_in(client: TestClient) -> None:
    body = {
        "runtime": "nanoclaw",
        "image": "argus-nanoclaw-host:dev",
        "desired_replicas": 0,
        "max_replicas": 1,
        "docker": {"runtime": "sysbox-runc"},
        "nanoclaw": {
            "install_dir": "/opt/nanoclaw",
            "instances_root": "/srv/nc-i",
            "agent_image": "nanoclaw-agent:1",
            "agent_image_archive": "/srv/agent.tar",
        },
    }
    res = client.put("/api/pools/nc-box", json=body, headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["nanoclaw"]["docker_socket"] is None


def test_pool_upsert_model_rejects_runtime_socket_conflict() -> None:
    with pytest.raises(ValidationError, match=r"docker_socket"):
        PoolUpsert(
            runtime="nanoclaw",
            image="h",
            max_replicas=1,
            docker=DockerOptions(runtime="sysbox-runc"),
            nanoclaw=NanoclawOptions(
                install_dir="/opt/nc",
                instances_root="/srv/i",
                agent_image="a:1",
                docker_socket="/var/run/docker.sock",
            ),
        )


def test_prepare_dir_uses_host_side_on_local_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    work = tmp_path / "instances" / "i-1"
    mount = Mount(
        str(work), str(work), "rw", create_owner=f"{os.getuid()}:{os.getgid()}"
    )
    target = DockerTarget(host=None)
    client = MagicMock()
    target._prepare_dir(client, "img:1", mount)
    assert work.is_dir()
    assert client.containers.run.call_count == 0


def test_prepare_dir_remote_avoids_shell() -> None:
    target = DockerTarget(host="tcp://remote:2375")
    client = MagicMock()
    mount = Mount("/srv/nc-i/i-1", "/srv/nc-i/i-1", "rw", create_owner="1000:1000")
    target._prepare_dir(client, "img:1", mount)
    assert client.containers.run.call_count == 2
    for call in client.containers.run.call_args_list:
        kwargs = call.kwargs
        assert kwargs["entrypoint"][0] in ("/bin/mkdir", "/bin/chown")
        assert "sh" not in kwargs["entrypoint"]
        assert kwargs["network_mode"] == "none"
        assert kwargs["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in kwargs["security_opt"]
        assert kwargs["volumes"] == {"/srv/nc-i": {"bind": "/srv/nc-i", "mode": "rw"}}


def test_docker_start_passes_hardening(monkeypatch: pytest.MonkeyPatch) -> None:
    from argus_api.fleet.targets import ContainerSpec

    runs: list[dict] = []

    class FakeContainers:
        def get(self, name):
            from docker.errors import NotFound

            raise NotFound("missing")

        def run(self, image, **kwargs):
            runs.append({"image": image, **kwargs})
            return MagicMock()

    class FakeImages:
        def get(self, image):
            return MagicMock()

    class FakeClient:
        containers = FakeContainers()
        images = FakeImages()

    target = DockerTarget(host=None)
    monkeypatch.setattr(target, "_docker", lambda: FakeClient())
    spec = ContainerSpec(
        instance_id="11111111-2222-3333-4444-555555555555",
        image="worker:1",
        env={},
        labels={"argus.pool": "web", "argus.instance_id": "x", "argus.managed": "true"},
    )
    assert target._start(spec) == spec.name
    assert len(runs) == 1
    assert runs[0]["cap_drop"] == ["ALL"]
    assert runs[0]["security_opt"] == ["no-new-privileges:true"]
    assert runs[0]["read_only"] is True
    assert runs[0]["pids_limit"] == 2048
    assert runs[0]["tmpfs"] == RUNTIME_TMPFS


def _socket_pool(**overrides: object):
    from datetime import UTC, datetime

    from argus_core.fleet import Pool

    body = {**SOCKET_POOL, **overrides}
    upsert = PoolUpsert.model_validate(body, context={"allow_docker_socket": True})
    now = datetime.now(UTC)
    return Pool(
        name="nc",
        status="ok",
        last_error=None,
        live_instances=0,
        created_at=now,
        updated_at=now,
        version="v1",
        **upsert.model_dump(),
    )


def test_socket_nanoclaw_publishes_the_custom_bridge_port() -> None:
    """Agent containers run on the default bridge, the NanoClaw host on its own
    network: the custom-harness bridge is reachable only through a port published
    on the bridge gateway (it was refused at host.docker.internal before)."""
    from argus_api.fleet.runtimes import CUSTOM_BRIDGE_PORT, container_spec
    from argus_api.fleet.targets import HOST_GATEWAY, container_name

    spec = container_spec(_socket_pool(), "i-1", ["argus_boot_x"], "http://argus")
    assert spec.ports == {f"{CUSTOM_BRIDGE_PORT}/tcp": (HOST_GATEWAY, None)}
    assert spec.env["ARGUS_CUSTOM_BRIDGE_PORT"] == str(CUSTOM_BRIDGE_PORT)
    assert spec.env["ARGUS_CUSTOM_BRIDGE_CONTAINER"] == container_name("i-1")


def test_docker_start_publishes_on_the_bridge_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argus_api.fleet.targets import HOST_GATEWAY, ContainerSpec

    runs: list[dict] = []

    class FakeContainers:
        def get(self, name):
            from docker.errors import NotFound

            raise NotFound("missing")

        def run(self, image, **kwargs):
            runs.append(kwargs)
            return MagicMock()

    class FakeNetworks:
        def get(self, name):
            assert name == "bridge"
            net = MagicMock()
            net.attrs = {
                "IPAM": {
                    "Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]
                }
            }
            return net

    class FakeClient:
        containers = FakeContainers()
        images = MagicMock()
        networks = FakeNetworks()

    target = DockerTarget(host=None)
    monkeypatch.setattr(target, "_docker", lambda: FakeClient())
    spec = ContainerSpec(
        instance_id="11111111-2222-3333-4444-555555555555",
        image="worker:1",
        env={},
        labels={},
        ports={"3141/tcp": (HOST_GATEWAY, None)},
    )
    target._start(spec)
    assert runs[0]["ports"] == {"3141/tcp": ("172.17.0.1", None)}


def test_docker_start_without_ports_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argus_api.fleet.targets import ContainerSpec

    runs: list[dict] = []

    class FakeContainers:
        def get(self, name):
            from docker.errors import NotFound

            raise NotFound("missing")

        def run(self, image, **kwargs):
            runs.append(kwargs)
            return MagicMock()

    class FakeClient:
        containers = FakeContainers()
        images = MagicMock()

    target = DockerTarget(host=None)
    monkeypatch.setattr(target, "_docker", lambda: FakeClient())
    target._start(ContainerSpec(instance_id="i-2", image="worker:1", env={}, labels={}))
    assert runs[0]["ports"] is None
