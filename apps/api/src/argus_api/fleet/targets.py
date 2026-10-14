"""Targets: *where* containers run (worker lifecycle 3.5).

Every call may fail or hang; callers treat failures as "unknown", never as
confirmation. Containers are named `argus-<instance id>`, which makes `start`
idempotent: a retried or ambiguous start finds the container instead of
creating a second one.
"""

import asyncio
import builtins
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, Protocol

MANAGED_LABEL = "argus.managed"
POOL_LABEL = "argus.pool"
INSTANCE_LABEL = "argus.instance_id"


class TargetError(Exception):
    """The provider could not be reached or refused the call."""


def container_name(instance_id: str) -> str:
    return f"argus-{instance_id}"


@dataclass(frozen=True)
class ContainerSpec:
    instance_id: str
    image: str
    env: dict[str, str]
    labels: dict[str, str]
    cpus: float | None = None
    memory_mb: int | None = None
    network: str | None = None
    extra_hosts: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return container_name(self.instance_id)


Status = Literal["created", "running", "exited"]
_STATUS: dict[str, Status] = {
    "created": "created",
    "running": "running",
    "paused": "running",
    "restarting": "running",
}


@dataclass(frozen=True)
class ProviderInstance:
    instance_id: str
    ref: str
    status: Status
    exit_code: int | None = None


class Target(Protocol):
    async def start(self, spec: ContainerSpec) -> str: ...
    async def stop(self, ref: str, *, grace: timedelta) -> None: ...
    async def list(self, labels: Mapping[str, str]) -> list[ProviderInstance]: ...
    async def logs(self, ref: str, *, tail: int) -> str: ...


class DockerTarget:
    """The Docker Engine API, local or remote (`unix://`, `ssh://`, `tcp://`).

    docker-py is synchronous, so every call runs in a worker thread.
    """

    def __init__(self, host: str | None = None) -> None:
        self.host = host
        self._client = None

    def _docker(self):
        import docker

        if self._client is None:
            if self.host is None:
                self._client = docker.from_env()
            else:
                self._client = docker.DockerClient(
                    base_url=self.host, use_ssh_client=self.host.startswith("ssh://")
                )
        return self._client

    async def start(self, spec: ContainerSpec) -> str:
        return await self._call(self._start, spec)

    async def stop(self, ref: str, *, grace: timedelta) -> None:
        await self._call(self._stop, ref, int(grace.total_seconds()))

    async def list(self, labels: Mapping[str, str]) -> list[ProviderInstance]:
        return await self._call(self._list, dict(labels))

    async def logs(self, ref: str, *, tail: int) -> str:
        return await self._call(self._logs, ref, tail)

    async def _call(self, fn, *args):
        from docker.errors import DockerException

        try:
            return await asyncio.to_thread(fn, *args)
        except DockerException as exc:
            raise TargetError(str(exc)[:500]) from exc

    def _start(self, spec: ContainerSpec) -> str:
        from docker.errors import APIError, ImageNotFound, NotFound

        client = self._docker()
        try:
            existing = client.containers.get(spec.name)
        except NotFound:
            existing = None
        if existing is not None:  # a retried or ambiguous start: never a second one
            if existing.status == "created":
                existing.start()
            return spec.name
        try:
            client.images.get(spec.image)
        except ImageNotFound:
            client.images.pull(spec.image)
        try:
            client.containers.run(
                spec.image,
                name=spec.name,
                detach=True,
                environment=spec.env,
                labels=spec.labels,
                network=spec.network,
                extra_hosts=_extra_hosts(spec.extra_hosts),
                nano_cpus=int(spec.cpus * 1e9) if spec.cpus else None,
                mem_limit=f"{spec.memory_mb}m" if spec.memory_mb else None,
            )
        except APIError as exc:
            if exc.status_code != 409:  # 409: created concurrently under our name
                raise
        return spec.name

    def _stop(self, ref: str, grace: int) -> None:
        from docker.errors import NotFound

        try:
            container = self._docker().containers.get(ref)
        except NotFound:
            return
        try:
            container.stop(timeout=grace)
            container.remove(force=True)
        except NotFound:
            return

    def _list(self, labels: dict[str, str]) -> builtins.list[ProviderInstance]:
        filters = {"label": [f"{k}={v}" for k, v in labels.items()]}
        result = []
        for c in self._docker().containers.list(all=True, filters=filters):
            instance_id = c.labels.get(INSTANCE_LABEL)
            if not instance_id:
                continue
            status: Status = _STATUS.get(c.status, "exited")
            exit_code = (
                c.attrs.get("State", {}).get("ExitCode") if status == "exited" else None
            )
            result.append(ProviderInstance(instance_id, c.name, status, exit_code))
        return result

    def _logs(self, ref: str, tail: int) -> str:
        from docker.errors import NotFound

        try:
            raw = self._docker().containers.get(ref).logs(tail=tail, timestamps=True)
        except NotFound:
            return ""
        return raw.decode(errors="replace")


def _extra_hosts(entries: list[str]) -> dict[str, str] | None:
    hosts = dict(e.split(":", 1) for e in entries if ":" in e)
    return hosts or None


class TargetRegistry:
    """One client per (kind, host), created on first use."""

    def __init__(self) -> None:
        self._targets: dict[tuple[str, str | None], Target] = {}

    def get(self, kind: str, host: str | None) -> Target:
        key = (kind, host)
        if key not in self._targets:
            if kind != "docker":
                raise TargetError(f"target {kind!r} is not supported yet")
            self._targets[key] = DockerTarget(host)
        return self._targets[key]

    def override(self, kind: str, host: str | None, target: Target) -> None:
        """For tests: route a target kind/host to a given implementation."""
        self._targets[(kind, host)] = target
