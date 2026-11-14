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


# docker.extra_networks (worker-launch spec): the Docker target joins them.


class _Net:
    def __init__(
        self, name: str, calls: list, *, connect_error: Exception | None = None
    ):
        self.name, self._calls, self._error = name, calls, connect_error

    def connect(self, container: object) -> None:
        self._calls.append(("connect", self.name, container))
        if self._error:
            raise self._error


class _Container:
    def __init__(self, calls: list, status: str = "created") -> None:
        self.status, self._calls = status, calls

    def start(self) -> None:
        self._calls.append(("start",))

    def remove(self, *, force: bool) -> None:
        self._calls.append(("remove", force))


class _Containers:
    def __init__(self, calls: list, existing: _Container | None, fresh: _Container):
        self._calls, self._existing, self._fresh = calls, existing, fresh

    def get(self, name: str) -> _Container:
        from docker.errors import NotFound

        if self._existing is None:
            raise NotFound("no such container")
        return self._existing

    def run(self, image: str, **kwargs: object) -> _Container:
        self._calls.append(("run", image, kwargs["network"]))
        return self._fresh


class _Networks:
    def __init__(self, calls: list, known: dict[str, _Net]) -> None:
        self._calls, self._known = calls, known

    def get(self, name: str) -> _Net:
        from docker.errors import NotFound

        self._calls.append(("get", name))
        if name not in self._known:
            raise NotFound(f"network {name} not found")
        return self._known[name]


class _Images:
    def get(self, image: str) -> object:
        return object()


def _docker_target(
    calls: list,
    nets: dict[str, _Net],
    *,
    existing: _Container | None = None,
    fresh: _Container | None = None,
) -> DockerTarget:
    from types import SimpleNamespace

    target = DockerTarget()
    target._client = SimpleNamespace(
        containers=_Containers(calls, existing, fresh or _Container(calls, "running")),
        networks=_Networks(calls, nets),
        images=_Images(),
    )
    return target


def _spec(**kw: object):
    from argus_api.fleet.targets import ContainerSpec

    return ContainerSpec("i-1", "img:1", {}, {}, network="primary", **kw)


def test_a_fresh_container_joins_each_extra_network_after_run() -> None:
    calls: list = []
    fresh = _Container(calls, "running")
    nets = {n: _Net(n, calls) for n in ("bridge", "side")}
    target = _docker_target(calls, nets, fresh=fresh)

    target._start(_spec(extra_networks=["bridge", "side"]))

    assert calls == [
        ("get", "bridge"),
        ("get", "side"),
        ("run", "img:1", "primary"),
        ("connect", "bridge", fresh),
        ("connect", "side", fresh),
    ]


def test_a_created_container_joins_extra_networks_before_it_starts() -> None:
    calls: list = []
    existing = _Container(calls, "created")
    target = _docker_target(calls, {"bridge": _Net("bridge", calls)}, existing=existing)

    target._start(_spec(extra_networks=["bridge"]))

    assert calls == [("get", "bridge"), ("connect", "bridge", existing), ("start",)]


def test_an_already_attached_network_is_tolerated() -> None:
    from docker.errors import APIError

    calls: list = []
    error = APIError("endpoint with name x already exists in network bridge")
    nets = {"bridge": _Net("bridge", calls, connect_error=error)}
    target = _docker_target(calls, nets, existing=_Container(calls, "created"))

    target._start(_spec(extra_networks=["bridge"]))  # a retried start: no error

    assert calls[-1] == ("start",)


def test_an_unknown_network_fails_before_anything_is_created() -> None:
    calls: list = []
    target = _docker_target(calls, {"bridge": _Net("bridge", calls)})

    with pytest.raises(targets.TargetError, match=r"'nope'.*does not exist"):
        target._start(_spec(extra_networks=["bridge", "nope"]))

    assert not any(c[0] in ("run", "connect", "start") for c in calls)


def test_a_failed_connect_removes_the_new_container() -> None:
    from docker.errors import APIError

    calls: list = []
    fresh = _Container(calls, "running")
    nets = {"bridge": _Net("bridge", calls, connect_error=APIError("boom"))}
    target = _docker_target(calls, nets, fresh=fresh)

    with pytest.raises(APIError):
        target._start(_spec(extra_networks=["bridge"]))

    assert calls[-1] == ("remove", True)
