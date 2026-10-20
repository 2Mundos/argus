"""Pools and instances (worker lifecycle 3.5)."""

from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus_core.agents import AgentRuntime

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
    # Explicit project -> HTTPS repository allowlist; values are not secrets.
    repositories: dict[str, str] = {}
    user: str | None = None  # "uid:gid" to run the host container as
    group_add: list[str] = []  # e.g. the docker socket's group id

    @model_validator(mode="before")
    @classmethod
    def drop_retired_provider_lists(cls, data: object) -> object:
        """`account_providers` / `executor_providers` were allowlists that narrowed
        account selection. Persisted pools may still carry them; they are dropped
        on read so they can no longer restrict anything (account selection spec)."""
        if isinstance(data, dict):
            return {
                k: v
                for k, v in data.items()
                if k not in ("account_providers", "executor_providers")
            }
        return data

    @model_validator(mode="after")
    def validate_repositories(self) -> "NanoclawOptions":
        for project, raw in self.repositories.items():
            parsed = urlsplit(raw)
            parts = [part for part in parsed.path.split("/") if part]
            if (
                not project
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or len(parts) != 2
            ):
                raise ValueError(
                    "repositories must map project names to HTTPS owner/repository URLs"
                )
        return self


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
    template: str | None = Field(default=None, max_length=40)  # worker launch 3

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
    version: str  # updated_at as stored; PATCH takes it as expected_version


REDACTED = "***"


class PoolPatch(BaseModel):
    """A partial pool update (MCP operations 3.3). Fields left out are kept; name,
    target and runtime cannot change. `env` merges: a value sets a variable,
    null removes it, and variables not named are kept (values are write-only)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: str = Field(min_length=1)
    image: str | None = Field(default=None, min_length=1, max_length=500)
    capabilities: list[str] | None = None
    agents_per_instance: int | None = Field(default=None, ge=1, le=50)
    desired_replicas: int | None = Field(default=None, ge=0)
    max_replicas: int | None = Field(default=None, ge=0, le=1000)
    env: dict[str, str | None] | None = None
    cpus: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, ge=16)
    docker: DockerOptions | None = None
    kubernetes: KubernetesOptions | None = None
    nanoclaw: NanoclawOptions | None = None


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
    capabilities: list[str] = []
    last_heartbeat_at: datetime | None = None
    runtime: AgentRuntime | None = None


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
    image: str | None = None  # what it was started with
    image_digest: str | None = None  # as the target reported it once running


class FactSource(StrEnum):
    """How far a runtime fact is proven, weakest first."""

    CONFIGURED = "configured"  # the pool definition says so
    REQUESTED = "requested"  # Argus asked the target for it
    STARTED = "started"  # the target reports it on the running container
    VALIDATED = "validated"  # a registered, heartbeating agent reports it
    UNKNOWN = "unknown"  # no proof either way


class Fact(BaseModel):
    value: str | bool | list[str] | None
    source: FactSource


class InstanceRuntime(BaseModel):
    image: Fact
    image_digest: Fact
    worker_version: Fact
    capabilities: Fact
    engine: Fact
    executor: Fact
    ready: Fact  # an active agent heartbeats: a live process, not a working model


class InspectedInstance(Instance):
    runtime: InstanceRuntime


class PoolInspection(BaseModel):
    pool: Pool
    instances: list[InspectedInstance]  # live ones, newest first


class Check(BaseModel):
    """One measured fact about a pool's readiness. `ok` is None when it could not
    be verified from here (`detail` says why); only False blocks a launch."""

    name: str
    ok: bool | None
    detail: str


class TargetCapability(BaseModel):
    kind: TargetKind
    location: str | None  # Docker host / kube context (None: the local default)
    available: bool
    error: str | None
    version: str | None
    runtimes: list[str]  # OCI runtimes the daemon offers
    cpus: float | None
    memory_mb: int | None


class PoolCapability(BaseModel):
    name: str
    target: TargetKind
    runtime: RuntimeKind
    launchable: bool
    free_slots: int
    checks: list[Check]
    blockers: list[str]


class FleetCapabilities(BaseModel):
    public_url_configured: bool
    targets: list[TargetCapability]
    pools: list[PoolCapability]


class LaunchRequest(BaseModel):
    # Compare-and-set against the pool's desired_replicas: guards double submits.
    expected_desired_replicas: int | None = Field(default=None, ge=0)


class LaunchResult(BaseModel):
    pool: Pool
    requested: bool  # always True on success: the reconciler starts it next pass
    desired_replicas: int


# -- Guided onboarding: templates (worker launch 3) ---------------------------------

TEMPLATE_DEFAULT_MAX_REPLICAS = 3
TEMPLATE_MAX_QUANTITY = 20


class NodeOption(BaseModel):
    id: str  # "local", or a configured Docker host
    label: str
    default: bool
    available: bool
    error: str | None
    version: str | None
    runtimes: list[str]
    cpus: float | None
    memory_mb: int | None
    shares_filesystem: bool  # host paths can be checked from this process


class ImageChoice(BaseModel):
    image: str
    capabilities: list[str]
    source: str  # "label" (on the node's daemon) or "config" (operator list)


class Readiness(BaseModel):
    node: str
    ready: bool
    checks: list[Check]
    missing: list[str]  # what to fix, for every check that is not verified true
    images: list[ImageChoice]  # argus-worker only: the offered images


class TemplateDefaults(BaseModel):
    name: str  # suggested, unique among existing pools
    quantity: int
    max_replicas: int
    capabilities: list[str]
    extra_hosts: list[str]


class WorkerTemplate(BaseModel):
    id: str
    name: str
    description: str
    runtime: RuntimeKind
    notes: list[str]  # security and behaviour the user should know
    # Set when using the template accepts a security risk: shown before submit and
    # to be acknowledged by the person choosing it.
    warning: str | None
    setup: list[str]  # what must exist on the node, always shown
    defaults: TemplateDefaults
    readiness: list[Readiness]  # one per node
    ready: bool  # ready on at least one node


class TemplateCatalog(BaseModel):
    public_url_configured: bool
    default_node: str
    nodes: list[NodeOption]
    templates: list[WorkerTemplate]
    pools: int  # how many pools exist (0: a fresh install)


class AdvancedOptions(BaseModel):
    """Admin-only overrides on top of a template's defaults."""

    capabilities: list[str] | None = None
    env: dict[str, str] | None = None
    extra_hosts: list[str] | None = None
    max_replicas: int | None = Field(default=None, ge=1, le=1000)


class CreateWorkerRequest(BaseModel):
    template: str = Field(min_length=1, max_length=40)
    node: str = Field(default="local", min_length=1, max_length=300)
    # Required: the pool name is the idempotency key, so a retry cannot create a
    # second pool. The catalog suggests a free one.
    name: str = Field(pattern=POOL_NAME_PATTERN)
    quantity: int = Field(default=1, ge=1, le=TEMPLATE_MAX_QUANTITY)
    image: str | None = Field(default=None, max_length=500)  # argus-worker only
    advanced: AdvancedOptions | None = None


class CreateWorkerResult(BaseModel):
    pool: Pool
    requested: int
    desired_replicas: int
