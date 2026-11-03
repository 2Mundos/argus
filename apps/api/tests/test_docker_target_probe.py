"""Whether a Docker daemon provably shares this process's filesystem.

Host paths are verified only then (worker-launch spec). A local unix socket does;
Docker Desktop for WSL 2 does too. On macOS the daemon runs in a VM with its own
paths, and a remote daemon never shares this filesystem.
"""

import pytest
from argus_api.fleet import targets
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
    ("host", "operating_system", "system", "shares"),
    [
        (None, "Ubuntu 24.04", "Linux", True),
        (None, "Docker Desktop", "Linux", True),  # Docker Desktop for WSL 2
        ("unix:///var/run/docker.sock", "Docker Desktop", "Linux", True),
        (None, "Docker Desktop", "Darwin", False),  # macOS: the daemon runs in a VM
        ("tcp://10.0.0.5:2376", "Ubuntu 24.04", "Linux", False),
        ("ssh://ops@build-box", "Ubuntu 24.04", "Linux", False),
    ],
)
async def test_a_local_unix_socket_shares_the_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    host: str | None,
    operating_system: str,
    system: str,
    shares: bool,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(targets.platform, "system", lambda: system)
    target = DockerTarget(host)
    target._client = _StubClient(operating_system)

    info = await target.probe()

    assert info.shares_filesystem is shares
