"""NanoclawOptions.onecli_url allowlist (security-hardening spec)."""

from datetime import UTC, datetime

import pytest
from argus_core.fleet import (
    IN_CLUSTER,
    DockerOptions,
    KubernetesOptions,
    NanoclawOptions,
    Pool,
    PoolUpsert,
    onecli_url_ok,
)
from pydantic import ValidationError

_BASE = {
    "install_dir": "/opt/nc",
    "instances_root": "/srv/i",
    "agent_image": "agent:1",
}

_ONECLI_DOCKER = "http://onecli:10254"

_PERSISTED_NANOCLAW_POOL = {
    "name": "nc",
    "target": "docker",
    "runtime": "nanoclaw",
    "image": "host:dev",
    "capabilities": [],
    "agents_per_instance": 1,
    "desired_replicas": 1,
    "max_replicas": 2,
    "env": {},
    "allow_mutable_images": False,
    "docker": {"network": "onecli-control"},
    "kubernetes": None,
    "nanoclaw": {
        "install_dir": "/opt/nc",
        "instances_root": "/srv/i",
        "agent_image": "a:1",
        "onecli_url": _ONECLI_DOCKER,
    },
    "template": None,
    "status": "ok",
    "last_error": None,
    "live_instances": 0,
    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    "version": "2026-01-01T00:00:00Z",
}


def _nanoclaw_pool_upsert(**docker: object) -> PoolUpsert:
    return PoolUpsert(
        runtime="nanoclaw",
        image="host:dev",
        desired_replicas=1,
        max_replicas=2,
        docker=DockerOptions(**docker),
        nanoclaw=NanoclawOptions(**_BASE, onecli_url=_ONECLI_DOCKER),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://host.docker.internal:10254",
        "http://127.0.0.1:10254",
        "http://localhost:10254",
        "http://onecli:10254",
        "http://onecli:10254/",
        "https://onecli.example.com",
        "https://onecli.example.com:8443/path",
    ],
)
def test_onecli_url_accepted(url: str) -> None:
    assert onecli_url_ok(url)
    assert NanoclawOptions(**_BASE, onecli_url=url).onecli_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/x",
        "http://onecli.evil:10254",
        "http://evilonecli:10254",
        "http://onecli:10255",
        "http://onecli:abc",
        "http://onecli:10254/extra",
        "http://onecli:10254?x=1",
        "http://onecli:10254#frag",
        "https://user:pass@onecli.example.com",
        "http://user@127.0.0.1:10254",
        "http://user@onecli:10254",
        "ftp://host.docker.internal:10254",
    ],
)
def test_onecli_url_rejected(url: str) -> None:
    assert not onecli_url_ok(url)
    with pytest.raises(ValidationError, match=r"onecli_url"):
        NanoclawOptions(**_BASE, onecli_url=url)


def test_persisted_pool_with_onecli_docker_service_url_validates() -> None:
    pool = Pool.model_validate(_PERSISTED_NANOCLAW_POOL)
    assert pool.nanoclaw is not None
    assert pool.nanoclaw.onecli_url == _ONECLI_DOCKER
    assert pool.docker.network == "onecli-control"


def test_onecli_docker_service_url_accepts_user_defined_network() -> None:
    pool = _nanoclaw_pool_upsert(network="onecli-control")
    assert pool.docker.network == "onecli-control"


@pytest.mark.parametrize(
    "network",
    ["", "bridge", "BRIDGE", "default", "host", "none"],
)
def test_onecli_docker_service_url_rejects_builtin_network(network: str) -> None:
    with pytest.raises(ValidationError, match=r"user-defined docker\.network"):
        _nanoclaw_pool_upsert(network=network)


def test_onecli_docker_service_url_rejects_missing_network() -> None:
    with pytest.raises(ValidationError, match=r"user-defined docker\.network"):
        _nanoclaw_pool_upsert()


def test_onecli_docker_service_url_rejects_kubernetes_target() -> None:
    with pytest.raises(ValidationError, match=r"target=docker"):
        PoolUpsert(
            target="kubernetes",
            runtime="nanoclaw",
            image="host:dev",
            desired_replicas=1,
            max_replicas=2,
            docker=DockerOptions(network="onecli-control"),
            kubernetes=KubernetesOptions(context=IN_CLUSTER),
            nanoclaw=NanoclawOptions(**_BASE, onecli_url=_ONECLI_DOCKER),
        )
