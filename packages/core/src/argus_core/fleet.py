"""Pools and instances (worker lifecycle 3.5)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

POOL_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"


class TargetKind(StrEnum):
    DOCKER = "docker"  # local socket or remote host (DOCKER_HOST ssh:// or tcp://)
    KUBERNETES = "kubernetes"


class RuntimeKind(StrEnum):
    PLAIN = "plain"  # any image that speaks the Argus API itself
    NANOCLAW = "nanoclaw"


class DockerOptions(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    host: str | None = None  # None: the Argus process's DOCKER_HOST / local socket
    network: str | None = None
    extra_hosts: list[str] = []  # e.g. "host.docker.internal:host-gateway"
    # OCI runtime for the container, e.g. "sysbox-runc". For the nanoclaw runtime
    # it replaces the shared Docker socket with a daemon inside the instance.
    runtime: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]*$")


IN_CLUSTER = "in-cluster"


class KubernetesOptions(BaseModel):
    """Where and how the `kubernetes` target runs an instance (one Pod each)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    # Required: a kubeconfig context name, or "in-cluster" when Argus itself runs
    # in the cluster. Argus never falls back to the kubeconfig's current context:
    # a worker pool must not land on whichever cluster happened to be selected.
    context: str = Field(min_length=1)
    namespace: str = Field(
        default="default", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63
    )
    service_account: str | None = None
    image_pull_secrets: list[str] = []
    node_selector: dict[str, str] = {}


# 107 (sun_path limit) - 1 ("/") - 36 (instance uuid) - len("/data/ncl.sock") = 56.
NANOCLAW_MAX_ROOT_LEN = 107 - 1 - 36 - len("/data/ncl.sock")


class NanoclawOptions(BaseModel):
    """The `nanoclaw` runtime (docs/research/2026-10-14.nanoclaw-integration).

    NanoClaw bind-mounts paths from its own filesystem into agent containers, so
    every path here is an absolute path **on the Docker host** and is mounted at
    the identical path inside the NanoClaw host container.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    # A NanoClaw checkout, built (`pnpm run build`) with the Argus channel installed.
    install_dir: str = Field(pattern=r"^/")
    # Parent of the per-instance working directories (data/, groups/, store/).
    # Short: NanoClaw binds Unix sockets at <root>/<instance id>/data/ncl.sock and
    # the OS limits socket paths to ~107 bytes (longer fails as "EADDRINUSE").
    instances_root: str = Field(pattern=r"^/", max_length=NANOCLAW_MAX_ROOT_LEN)
    # The NanoClaw agent image, already present on that Docker daemon.
    agent_image: str = Field(min_length=1)
    onecli_url: str = "http://host.docker.internal:10254"
    docker_socket: str = Field(default="/var/run/docker.sock", pattern=r"^/")
    # A `docker save` archive on the Docker host, loaded into the instance's own
    # daemon when `docker.runtime` is set (that daemon starts without images).
    # Absent: `agent_image` is pulled from its registry.
    agent_image_archive: str | None = Field(default=None, pattern=r"^/")
    # Providers whose accounts Argus leases to this agent and switches in the
    # OneCLI vault (e.g. ["anthropic"]). Empty: use the vault's own credential.
    account_providers: list[str] = []
    user: str | None = None  # "uid:gid" to run the host container as
    group_add: list[str] = []  # e.g. the docker socket's group id


class PoolUpsert(BaseModel):
    target: TargetKind = TargetKind.DOCKER
    runtime: RuntimeKind = RuntimeKind.PLAIN
    image: str = Field(min_length=1, max_length=500)
    capabilities: list[str] = []
    agents_per_instance: int = Field(default=1, ge=1, le=50)
    desired_replicas: int = Field(default=0, ge=0)
    max_replicas: int = Field(ge=0, le=1000)
    env: dict[str, str] = {}
    cpus: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, ge=16)
    docker: DockerOptions = DockerOptions()
    kubernetes: KubernetesOptions | None = None  # required for the kubernetes target
    nanoclaw: NanoclawOptions | None = None  # required for the nanoclaw runtime

    @model_validator(mode="after")
    def consistent(self) -> "PoolUpsert":
        if self.desired_replicas > self.max_replicas:
            raise ValueError("desired_replicas cannot exceed max_replicas")
        if self.target == TargetKind.KUBERNETES:
            if self.kubernetes is None:
                raise ValueError("the kubernetes target needs the `kubernetes` options")
            if self.runtime != RuntimeKind.PLAIN:
                raise ValueError("only the plain runtime runs on the kubernetes target")
        if self.runtime == RuntimeKind.NANOCLAW:
            if self.nanoclaw is None:
                raise ValueError("the nanoclaw runtime needs the `nanoclaw` options")
            if self.agents_per_instance != 1:
                raise ValueError("the nanoclaw runtime runs one agent per instance")
            if self.docker.runtime and (self.nanoclaw.user or self.nanoclaw.group_add):
                raise ValueError(
                    "with docker.runtime the host runs as root inside its sandbox:"
                    " leave nanoclaw.user and nanoclaw.group_add empty"
                )
        elif self.docker.runtime and self.target == TargetKind.KUBERNETES:
            raise ValueError("docker.runtime applies to the docker target")
        return self


class PoolStatus(StrEnum):
    OK = "ok"
    UNREACHABLE = "unreachable"  # the target could not be queried
    FAILING = "failing"  # starts keep failing (e.g. a bad image)


class Pool(PoolUpsert):
    # Responses always carry every field, so the generated client need not
    # treat defaulted ones as optional.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    name: str
    status: PoolStatus
    last_error: str | None
    live_instances: int  # every instance not confirmed terminated
    created_at: datetime
    updated_at: datetime


class ScaleRequest(BaseModel):
    desired_replicas: int = Field(ge=0)


class ScaleResult(BaseModel):
    pool: Pool
    # Set when an orchestrator asked for more than max_replicas: a human decides.
    decision_id: str | None = None


class InstanceState(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


class InstanceAgent(BaseModel):
    id: str
    name: str
    state: str
    claimed_tasks: int


class Instance(BaseModel):
    id: str
    pool: str
    provider_ref: str
    state: InstanceState
    draining: bool
    start_attempts: int
    last_error: str | None
    exit_reason: str | None
    agents: list[InstanceAgent]
    created_at: datetime
    running_at: datetime | None
    terminating_at: datetime | None
    terminated_at: datetime | None
