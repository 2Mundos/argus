"""Whether a Docker daemon provably shares this process's filesystem.

Host paths are verified only then (worker-launch spec). A local unix socket does,
Docker Desktop included; a remote daemon does not.
"""

import pytest
from argus_api.fleet.targets import DockerTarget


class _StubClient:
    def __init__(self, operating_system: str) -> None:
        self._operating_system = operating_system

    def ping(self) -> bool:
        return True

    def info(self) -> dict:
        return {
            "OperatingSystem": self._operating_system,
            "ServerVersion": "29.5.2",
            "Runtimes": {"runc": {}},
            "NCPU": 4,
            "MemTotal": 8 * 1024**3,
        }


@pytest.mark.parametrize(
    ("host", "operating_system", "shares"),
    [
        (None, "Ubuntu 24.04", True),
        (None, "Docker Desktop", True),
        ("unix:///var/run/docker.sock", "Docker Desktop", True),
        ("tcp://10.0.0.5:2376", "Ubuntu 24.04", False),
        ("ssh://ops@build-box", "Ubuntu 24.04", False),
    ],
)
async def test_a_local_unix_socket_shares_the_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    host: str | None,
    operating_system: str,
    shares: bool,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    target = DockerTarget(host)
    target._client = _StubClient(operating_system)

    info = await target.probe()

    assert info.shares_filesystem is shares
