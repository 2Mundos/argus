"""Targets: *where* containers run (worker lifecycle 3.5).

Every call may fail or hang; callers treat failures as "unknown", never as
confirmation. Containers are named `argus-<instance id>`, which makes `start`
idempotent: a retried or ambiguous start finds the container instead of
creating a second one.
"""

import asyncio
import builtins
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

MANAGED_LABEL = "argus.managed"
POOL_LABEL = "argus.pool"
INSTANCE_LABEL = "argus.instance_id"

# Default hardening for every container Argus starts (worker lifecycle 3.8).
RUNTIME_TMPFS: dict[str, str] = {
    "/tmp": "rw,nosuid,nodev,size=64m",  # noqa: S108 — container scratch, not host temp
    "/var/tmp": "rw,nosuid,nodev,size=16m",  # noqa: S108
    "/run": "rw,nosuid,nodev,size=16m",
}
# Sandboxed NanoClaw runs an inner dockerd on a read-only root; give it scratch.
SANDBOX_TMPFS: dict[str, str] = {
    **RUNTIME_TMPFS,
    "/var/lib/docker": "rw,nosuid,nodev,size=2g",
    "/var/log": "rw,nosuid,nodev,size=64m",
    "/var/run": "rw,nosuid,nodev,size=16m",
}


class TargetError(Exception):
    """The provider could not be reached or refused the call."""


def container_name(instance_id: str) -> str:
    return f"argus-{instance_id}"


@dataclass(frozen=True)
class Mount:
    host: str
    container: str
    mode: Literal["ro", "rw"] = "rw"
    # `uid:gid`: create the host directory with this owner before starting.
    # Docker would otherwise create a missing source as root.
    create_owner: str | None = None


@dataclass(frozen=True)
class KubernetesPlacement:
    namespace: str
    service_account: str | None = None
    image_pull_secrets: list[str] = field(default_factory=list)
    node_selector: dict[str, str] = field(default_factory=dict)


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
    mounts: list[Mount] = field(default_factory=list)
    user: str | None = None
    group_add: list[str] = field(default_factory=list)
    runtime: str | None = None  # OCI runtime (Docker target), e.g. sysbox-runc
    # Hardening applied on every Docker start (and mirrored on Kubernetes pods).
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    security_opt: list[str] = field(default_factory=lambda: ["no-new-privileges:true"])
    read_only: bool = True
    pids_limit: int | None = 2048
    tmpfs: dict[str, str] = field(default_factory=lambda: dict(RUNTIME_TMPFS))
    kubernetes: KubernetesPlacement | None = None

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
    image_digest: str | None = None  # the image the container actually runs


@dataclass(frozen=True)
class LabeledContainer:
    ref: str
    value: str  # the value of the label that was asked for
    running: bool


@dataclass(frozen=True)
class TargetInfo:
    """What a probe of the target measured."""

    version: str | None = None
    runtimes: list[str] = field(default_factory=list)
    cpus: float | None = None
    memory_mb: int | None = None
    # True only if the daemon provably uses this process's filesystem (a local
    # unix socket, not a VM such as Docker Desktop): then host paths can be checked.
    shares_filesystem: bool = False


@dataclass(frozen=True)
class LabeledImage:
    tag: str
    labels: dict[str, str]


class Target(Protocol):
    async def probe(self) -> TargetInfo:
        """Read-only reachability check; raises TargetError when unreachable."""
        ...

    async def image_present(self, image: str) -> bool | None:
        """Whether the image is already on the target; None: cannot tell."""
        ...

    async def labeled_images(self, key: str) -> builtins.list[LabeledImage]:
        """Installed images carrying label `key` (one entry per tag)."""
        ...

    async def start(self, spec: ContainerSpec) -> str: ...
    async def stop(self, ref: str, *, grace: timedelta) -> None: ...
    async def list(self, labels: Mapping[str, str]) -> list[ProviderInstance]: ...
    async def list_by_label(self, key: str) -> builtins.list[LabeledContainer]:
        """Every container (any state) carrying the label `key`, with its value."""
        ...

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

    async def list_by_label(self, key: str) -> builtins.list[LabeledContainer]:
        return await self._call(self._list_by_label, key)

    async def logs(self, ref: str, *, tail: int) -> str:
        return await self._call(self._logs, ref, tail)

    async def probe(self) -> TargetInfo:
        return await self._call(self._probe)

    async def image_present(self, image: str) -> bool | None:
        return await self._call(self._image_present, image)

    async def labeled_images(self, key: str) -> builtins.list[LabeledImage]:
        return await self._call(self._labeled_images, key)

    def _labeled_images(self, key: str) -> builtins.list[LabeledImage]:
        found = self._docker().images.list(filters={"label": [key]})
        return [
            LabeledImage(tag, dict(img.labels or {}))
            for img in found
            for tag in img.tags
        ]

    def _probe(self) -> TargetInfo:
        client = self._docker()
        client.ping()
        info = client.info()
        effective = self.host or os.environ.get("DOCKER_HOST")
        local = (effective is None or effective.startswith("unix://")) and (
            "desktop" not in str(info.get("OperatingSystem", "")).lower()
        )
        return TargetInfo(
            shares_filesystem=local,
            version=info.get("ServerVersion"),
            runtimes=sorted(info.get("Runtimes") or {}),
            cpus=info.get("NCPU"),
            memory_mb=(info.get("MemTotal") or 0) // (1024 * 1024) or None,
        )

    def _image_present(self, image: str) -> bool:
        from docker.errors import ImageNotFound

        try:
            self._docker().images.get(image)
        except ImageNotFound:
            return False
        return True

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
        for m in spec.mounts:
            if m.create_owner:
                self._prepare_dir(client, spec.image, m)
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
                volumes={
                    m.host: {"bind": m.container, "mode": m.mode} for m in spec.mounts
                }
                or None,
                user=spec.user,
                group_add=spec.group_add or None,
                runtime=spec.runtime,
                cap_drop=list(spec.cap_drop) or None,
                security_opt=list(spec.security_opt) or None,
                read_only=spec.read_only,
                pids_limit=spec.pids_limit,
                tmpfs=dict(spec.tmpfs) or None,
            )
        except APIError as exc:
            if exc.status_code != 409:  # 409: created concurrently under our name
                raise
        return spec.name

    def _prepare_dir(self, client: Any, image: str, mount: Mount) -> None:
        """Create a bind-mount source owned by the instance user.

        Prefer host-side mkdir/chown when this process shares the daemon's
        filesystem (local unix socket). For a remote daemon, run a one-shot
        container that mounts only the parent directory and invokes mkdir/chown
        as argv (no shell).
        """
        if not mount.create_owner:
            return
        if self._local_unix_daemon():
            self._prepare_dir_on_host(mount)
            return
        self._prepare_dir_remote(client, image, mount)

    def _local_unix_daemon(self) -> bool:
        effective = self.host or os.environ.get("DOCKER_HOST")
        return effective is None or effective.startswith("unix://")

    @staticmethod
    def _prepare_dir_on_host(mount: Mount) -> None:
        owner = mount.create_owner
        if owner is None:
            return
        path = Path(mount.host)
        path.mkdir(parents=True, exist_ok=True)
        uid_s, gid_s = owner.split(":", 1)
        os.chown(path, int(uid_s), int(gid_s))

    @staticmethod
    def _prepare_dir_remote(client: Any, image: str, mount: Mount) -> None:
        parent = str(Path(mount.host).parent)
        volumes = {parent: {"bind": parent, "mode": "rw"}}
        # Root is still required for chown across uids; drop every capability
        # except the two mkdir/chown need, no network, no new privileges.
        common = {
            "user": "0:0",
            "volumes": volumes,
            "remove": True,
            "network_mode": "none",
            "cap_drop": ["ALL"],
            "cap_add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
            "security_opt": ["no-new-privileges:true"],
            "read_only": True,
            "tmpfs": {"/tmp": "rw,nosuid,nodev,size=1m"},  # noqa: S108
        }
        client.containers.run(
            image, entrypoint=["/bin/mkdir", "-p", mount.host], **common
        )
        client.containers.run(
            image,
            entrypoint=["/bin/chown", mount.create_owner, mount.host],
            **common,
        )

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
            result.append(
                ProviderInstance(
                    instance_id, c.name, status, exit_code, c.attrs.get("Image")
                )
            )
        return result

    def _list_by_label(self, key: str) -> builtins.list[LabeledContainer]:
        found = self._docker().containers.list(all=True, filters={"label": [key]})
        return [
            LabeledContainer(
                c.name, c.labels[key], c.status in ("running", "paused", "restarting")
            )
            for c in found
            if key in c.labels
        ]

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


class KubernetesTarget:
    """One Pod per instance, in one namespace of one cluster.

    The client is built from an explicit kubeconfig context (or in-cluster
    config) and never mutates global kubeconfig state. Pods never restart on
    their own (`restartPolicy: Never`): replacing an instance is the
    reconciler's decision. A pod that is being deleted still lists as running
    until it is actually gone, so deletion is never mistaken for confirmation.
    """

    def __init__(self, context: str, namespace: str, api: Any = None) -> None:
        self.context = context
        self.namespace = namespace
        self._api_obj = api

    def _api(self) -> Any:
        if self._api_obj is None:
            from kubernetes import client, config

            if self.context == "in-cluster":
                config.load_incluster_config()
                api_client = client.ApiClient()
            else:
                api_client = config.new_client_from_config(context=self.context)
            self._api_obj = client.CoreV1Api(api_client)
        return self._api_obj

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        from kubernetes.client.exceptions import ApiException
        from kubernetes.config.config_exception import ConfigException

        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ApiException as exc:
            raise _KubeError(
                exc.status or 0, f"{exc.status} {exc.reason}: {_body(exc)}"
            ) from exc
        except (ConfigException, OSError) as exc:
            raise TargetError(f"kubernetes: {str(exc)[:300]}") from exc

    async def start(self, spec: ContainerSpec) -> str:
        pod = pod_manifest(spec, self.namespace)
        try:
            await self._call(self._api().create_namespaced_pod, self.namespace, pod)
        except _KubeError as exc:
            if exc.status != 409:  # 409: created by an earlier, ambiguous attempt
                raise TargetError(str(exc)) from exc
        return pod["metadata"]["name"]

    async def stop(self, ref: str, *, grace: timedelta) -> None:
        try:
            await self._call(
                self._api().delete_namespaced_pod,
                ref,
                self.namespace,
                grace_period_seconds=int(grace.total_seconds()),
            )
        except _KubeError as exc:
            if exc.status != 404:
                raise TargetError(str(exc)) from exc

    async def list(self, labels: Mapping[str, str]) -> builtins.list[ProviderInstance]:
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        pods = await self._list_pods(selector)
        return [
            ProviderInstance(
                p.metadata.labels[INSTANCE_LABEL],
                p.metadata.name,
                *pod_status(p),
                image_digest=pod_image_id(p),
            )
            for p in pods
            if INSTANCE_LABEL in (p.metadata.labels or {})
        ]

    async def list_by_label(self, key: str) -> builtins.list[LabeledContainer]:
        pods = await self._list_pods(key)
        return [
            LabeledContainer(
                p.metadata.name, p.metadata.labels[key], pod_status(p)[0] != "exited"
            )
            for p in pods
            if key in (p.metadata.labels or {})
        ]

    async def probe(self) -> TargetInfo:
        try:
            await self._call(self._api().list_namespaced_pod, self.namespace, limit=1)
        except _KubeError as exc:
            raise TargetError(str(exc)) from exc
        return TargetInfo()

    async def image_present(self, image: str) -> bool | None:
        return None  # nodes pull on demand; the cluster's cache is not visible

    async def labeled_images(self, key: str) -> builtins.list[LabeledImage]:
        return []

    async def _list_pods(self, selector: str) -> Any:
        try:
            result = await self._call(
                self._api().list_namespaced_pod, self.namespace, label_selector=selector
            )
        except _KubeError as exc:
            raise TargetError(str(exc)) from exc
        return result.items

    async def logs(self, ref: str, *, tail: int) -> str:
        try:
            # The client's own str deserialization mangles the bytes body
            # ("b'...'"), so read the raw response.
            resp = await self._call(
                self._api().read_namespaced_pod_log,
                ref,
                self.namespace,
                tail_lines=tail,
                timestamps=True,
                _preload_content=False,
            )
            return bytes(resp.data).decode(errors="replace")
        except _KubeError as exc:
            if exc.status == 404:
                return ""
            if exc.status == 400:  # not started yet: say why instead of failing
                return await self._why_not_started(ref)
            raise TargetError(str(exc)) from exc

    async def _why_not_started(self, ref: str) -> str:
        try:
            pod = await self._call(self._api().read_namespaced_pod, ref, self.namespace)
        except _KubeError:
            return ""
        return describe_waiting(pod)


class _KubeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _body(exc: Any) -> str:
    try:
        return str(json.loads(exc.body).get("message", exc.body))[:300]
    except (ValueError, TypeError, AttributeError):
        return str(exc.body)[:300]


def pod_manifest(spec: ContainerSpec, namespace: str) -> dict[str, Any]:
    k = spec.kubernetes or KubernetesPlacement(namespace)
    resources: dict[str, dict[str, str]] = {}
    if spec.cpus:
        resources.setdefault("limits", {})["cpu"] = f"{int(spec.cpus * 1000)}m"
    if spec.memory_mb:
        resources.setdefault("limits", {})["memory"] = f"{spec.memory_mb}Mi"
    if "limits" in resources:  # requests = limits: a predictable QoS class
        resources["requests"] = dict(resources["limits"])
    security: dict[str, Any] = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": spec.read_only,
        "capabilities": {"drop": list(spec.cap_drop) or ["ALL"]},
    }
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        # Workers talk to Argus, not to the cluster API.
        "automountServiceAccountToken": False,
        "containers": [
            {
                "name": "worker",
                "image": spec.image,
                "env": [{"name": n, "value": v} for n, v in spec.env.items()],
                "securityContext": security,
                **({"resources": resources} if resources else {}),
            }
        ],
    }
    if k.service_account:
        pod_spec["serviceAccountName"] = k.service_account
    if k.image_pull_secrets:
        pod_spec["imagePullSecrets"] = [{"name": n} for n in k.image_pull_secrets]
    if k.node_selector:
        pod_spec["nodeSelector"] = dict(k.node_selector)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.name,
            "namespace": namespace,
            "labels": dict(spec.labels),
        },
        "spec": pod_spec,
    }


def pod_status(pod: Any) -> tuple[Status, int | None]:
    """Map a Pod to the provider status the reconciler understands.

    Pending is `created`; Running (including a pod being deleted, which is not
    gone yet) is `running`; only Succeeded/Failed is `exited`.
    """
    phase = (pod.status.phase if pod.status else None) or "Unknown"
    if phase == "Pending":
        return "created", None
    if phase in ("Succeeded", "Failed"):
        code: int | None = 0 if phase == "Succeeded" else 1
        for cs in pod.status.container_statuses or []:
            term = cs.state.terminated if cs.state else None
            if term is not None:
                code = term.exit_code
                break
        return "exited", code
    return "running", None


def pod_image_id(pod: Any) -> str | None:
    """The image digest the kubelet resolved for the worker container, if any."""
    for cs in (pod.status.container_statuses if pod.status else None) or []:
        if image_id := getattr(cs, "image_id", None):
            return image_id
    return None


def describe_waiting(pod: Any) -> str:
    """Why a pod has no logs yet: the container's waiting reason, or its phase."""
    for cs in (pod.status.container_statuses or []) if pod.status else []:
        waiting = cs.state.waiting if cs.state else None
        if waiting is not None:
            return f"waiting: {waiting.reason}: {waiting.message or ''}".strip()
    for cond in (pod.status.conditions or []) if pod.status else []:
        if cond.status == "False" and cond.message:
            return f"{cond.type}: {cond.reason}: {cond.message}"
    return f"phase: {pod.status.phase if pod.status else 'unknown'}"


class TargetRegistry:
    """One client per (kind, host, namespace), created on first use.

    `host` is the Docker host, or the Kubernetes context; `namespace` is only
    used by the Kubernetes target.
    """

    def __init__(self) -> None:
        self._targets: dict[tuple[str, str | None, str | None], Target] = {}

    def get(self, kind: str, host: str | None, namespace: str | None = None) -> Target:
        key = (kind, host, namespace)
        if key not in self._targets:
            if kind == "docker":
                self._targets[key] = DockerTarget(host)
            elif kind == "kubernetes":
                if not host:
                    raise TargetError("the kubernetes target needs an explicit context")
                self._targets[key] = KubernetesTarget(host, namespace or "default")
            else:
                raise TargetError(f"target {kind!r} is not supported")
        return self._targets[key]

    def override(
        self, kind: str, host: str | None, target: Target, namespace: str | None = None
    ) -> None:
        """For tests: route a target kind/host to a given implementation."""
        self._targets[(kind, host, namespace)] = target
