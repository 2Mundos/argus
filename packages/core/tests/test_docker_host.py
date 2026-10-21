"""DockerOptions.host scheme allowlist (security-hardening spec)."""

import pytest
from argus_core.fleet import DockerOptions
from pydantic import ValidationError


@pytest.mark.parametrize(
    "host",
    [
        "unix:///var/run/docker.sock",
        "ssh://user@host",
        "tcp://remote:2376?tls=true",
        "tcp://remote:2376?tlsverify=true",
        "https://remote:2376",
    ],
)
def test_docker_host_accepted(host: str) -> None:
    assert DockerOptions(host=host).host == host


@pytest.mark.parametrize(
    "host",
    [
        "tcp://remote:2375",
        "tcp://remote:2376",
        "http://remote:2375",
        "ftp://remote",
    ],
)
def test_docker_host_rejected(host: str) -> None:
    with pytest.raises(ValidationError, match=r"docker host|tls"):
        DockerOptions(host=host)
