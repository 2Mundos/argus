"""Pools and instances (worker lifecycle 3.5)."""

from datetime import datetime
from enum import StrEnum
from urllib.parse import SplitResult, parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus_core.agents import AgentRuntime

POOL_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"


class TargetKind(StrEnum):
    DOCKER = "docker"  # local socket or remote host (DOCKER_HOST ssh:// or tcp://)
    KUBERNETES = "kubernetes"


class RuntimeKind(StrEnum):
    PLAIN = "plain"  # any image that speaks the Argus API itself
    NANOCLAW = "nanoclaw"


def _docker_host_ok(host: str) -> bool:
    """unix://, ssh://, https://, or tcp:// with TLS query params."""
    parsed = urlsplit(host)
    scheme = parsed.scheme.lower()
    if scheme in ("unix", "ssh", "https"):
        return True
    if scheme != "tcp":
        return False
    qs = {k.lower(): [x.lower() for x in v] for k, v in parse_qs(parsed.query).items()}
    for key in ("tls", "tlsverify"):
        if qs.get(key, [""])[0] in ("1", "true", "yes"):
            return True
    return False


class DockerOptions(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    host: str | None = None  # None: the Argus process's DOCKER_HOST / local socket
    network: str | None = None
    extra_hosts: list[str] = []  # e.g. "host.docker.internal:host-gateway"
    # OCI runtime for the container, e.g. "sysbox-runc". For the nanoclaw runtime
    # it replaces the shared Docker socket with a daemon inside the instance.
    runtime: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]*$")

    @field_validator("host")
    @classmethod
    def host_scheme(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _docker_host_ok(value):
            raise ValueError(
                "docker host must be unix://, ssh://, https://, or tcp:// with"
                " ?tls=true (plain tcp:// is refused)"
            )
        return value


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


_ONECLI_HTTP_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "[::1]", "host.docker.internal"}
)

# Private Docker DNS for the OneCLI admin service on a user-defined network
# (e.g. `onecli-control`). Not reachable from the public internet; HTTP is
# confined to this exact origin (host `onecli`, port 10254).
_ONECLI_DOCKER_SERVICE_HOST = "onecli"
_ONECLI_DOCKER_SERVICE_PORT = 10254

_DOCKER_BUILTIN_NETWORKS = frozenset({"bridge", "default", "host", "none"})


def _parsed_port(parsed: SplitResult) -> int | None:
    try:
        return parsed.port
    except ValueError:
        return None


def _docker_user_defined_network_ok(network: str | None) -> bool:
    name = (network or "").strip()
    if not name:
        return False
    return name.casefold() not in _DOCKER_BUILTIN_NETWORKS


def _onecli_private_docker_service_http_ok(parsed: SplitResult) -> bool:
    """``http://onecli:10254`` (optional trailing ``/``) on the pool Docker network."""
    if parsed.scheme != "http":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").lower()
    if host != _ONECLI_DOCKER_SERVICE_HOST:
        return False
    if _parsed_port(parsed) != _ONECLI_DOCKER_SERVICE_PORT:
        return False
    path = parsed.path or ""
    if path not in ("", "/"):
        return False
    return not (parsed.query or parsed.fragment)


def onecli_is_private_docker_service_url(url: str) -> bool:
    return _onecli_private_docker_service_http_ok(urlsplit(url))


def onecli_url_ok(url: str) -> bool:
    """https anywhere, or http to loopback / host.docker.internal / private Docker OneCLI."""
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return bool(host)
    if parsed.scheme == "http":
        if host in _ONECLI_HTTP_HOSTS:
            return True
        return _onecli_private_docker_service_http_ok(parsed)
    return False


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
    # Absolute path on the Docker host. Unset in sandboxed mode (`docker.runtime`);
    # unset in socket mode means `/var/run/docker.sock`.
    docker_socket: str | None = Field(default=None, pattern=r"^/")
    # A `docker save` archive on the Docker host, loaded into the instance's own
    # daemon when `docker.runtime` is set (that daemon starts without images).
    # Absent: `agent_image` is pulled from its registry.
    agent_image_archive: str | None = Field(default=None, pattern=r"^/")
    # Explicit project -> HTTPS repository allowlist; values are not secrets.
    repositories: dict[str, str] = {}
    user: str | None = None  # "uid:gid" to run the host container as
    group_add: list[str] = []  # e.g. the docker socket's group id

    @field_validator("onecli_url")
    @classmethod
    def onecli_url_scheme(cls, value: str) -> str:
        if not onecli_url_ok(value):
            raise ValueError(
                "onecli_url must be https://, or http:// to localhost /"
                " 127.0.0.1 / host.docker.internal / http://onecli:10254 on a"
                " private Docker network, without embedded credentials"
            )
        return value

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
    # When false (default), a tag-only image missing locally is refused; Argus
    # never silently re-pulls a mutable tag that is already present.
    allow_mutable_images: bool = False
    docker: DockerOptions = DockerOptions()
    kubernetes: KubernetesOptions | None = None  # required for the kubernetes target
    nanoclaw: NanoclawOptions | None = None  # required for the nanoclaw runtime
    template: str | None = Field(default=None, max_length=40)  # worker launch 3

    @model_validator(mode="after")
    def consistent(self) -> "PoolUpsert":
        if self.desired_replicas > self.max_replicas:
            raise ValueError("desired_replicas cannot exceed max_replicas")
        if (
            self.runtime == RuntimeKind.NANOCLAW
            and self.nanoclaw is not None
            and onecli_is_private_docker_service_url(self.nanoclaw.onecli_url)
        ):
            if self.target != TargetKind.DOCKER:
                raise ValueError(
                    "http://onecli:10254 requires target=docker with a user-defined"
                    " docker.network (e.g. onecli-control)"
                )
            if not _docker_user_defined_network_ok(self.docker.network):
                raise ValueError(
                    "http://onecli:10254 requires a user-defined docker.network"
                    " (not bridge, default, host, or none; e.g. onecli-control)"
                )
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
            if self.docker.runtime:
                if self.nanoclaw.user or self.nanoclaw.group_add:
                    raise ValueError(
                        "with docker.runtime the host runs as root inside its sandbox:"
                        " leave nanoclaw.user and nanoclaw.group_add empty"
                    )
                if self.nanoclaw.docker_socket is not None:
                    raise ValueError(
                        "with docker.runtime the host has no Docker socket:"
                        " leave nanoclaw.docker_socket unset"
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
    allow_mutable_images: bool | None = None
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


class RuntimeImage(BaseModel):
    """An image the operator configured for a NanoClaw template, with what was
    measured about it on the node. `present` is None when it cannot be checked."""

    image: str
    source: str  # "config": the ARGUS_NANOCLAW_* setting named in `setting`
    setting: str
    present: bool | None
    detail: str


class Readiness(BaseModel):
    node: str
    ready: bool
    checks: list[Check]
    missing: list[str]  # what to fix, for every check that is not verified true
    images: list[ImageChoice]  # argus-worker only: the offered images
    # NanoClaw templates only: the Docker image of the runtime host, and the
    # NanoClaw agent image the host starts. Two different images; None if unset.
    host_image: RuntimeImage | None = None
    agent_image: RuntimeImage | None = None


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


class WorkspaceCleanupStatus(StrEnum):
    """Audit status of one instances-root entry (workspace-cleanup.spec.md 3.2)."""

    ELIGIBLE = "eligible"
    PRESERVED = "preserved"
    BLOCKED = "blocked"
    CLEANED = "cleaned"
    FAILED = "failed"


class WorkspaceCleanupEntry(BaseModel):
    root: str
    name: str  # the directory name; the instance id when it is one
    instance_id: str | None
    pool: str | None
    status: WorkspaceCleanupStatus
    reason: str  # a reason code, never free text
    attempts: int
    bytes_estimated: int | None
    bytes_freed: int | None
    archive_path: str | None
    error: str | None  # sanitized: exception class and errno name only
    last_run_id: str
    first_seen_at: datetime
    updated_at: datetime
    cleaned_at: datetime | None


class WorkspaceCleanupPage(BaseModel):
    total: int
    items: list[WorkspaceCleanupEntry]


class WorkspaceCleanupRun(BaseModel):
    id: str
    trigger: str  # loop | manual
    actor: str
    dry_run: bool
    low_disk: bool
    status: str  # running | finished | failed | timed_out | interrupted
    started_at: datetime
    finished_at: datetime | None
    scanned: int
    eligible: int
    preserved: int
    blocked: int
    cleaned: int
    failed: int
    bytes_estimated: int
    # statvfs free-space delta around deletions: approximate, not `du`.
    bytes_freed: int
    free_before: int | None
    free_after: int | None
    skipped: list[str]  # roots or pools skipped as a whole, with a reason code
    error: str | None


class WorkspaceCleanupSettings(BaseModel):
    enabled: bool
    dry_run: bool
    archive_root: str | None
    min_age_hours: int
    interval_s: float
    run_timeout_s: float
    max_instances: int
    max_archive_bytes: int
    max_attempts: int
    low_disk_bytes: int


class WorkspaceCleanupSummary(BaseModel):
    settings: WorkspaceCleanupSettings
    running: bool
    last_run: WorkspaceCleanupRun | None
    counts: dict[WorkspaceCleanupStatus, int]
    bytes_estimated: int  # over rows that are not cleaned yet
    bytes_freed: int  # over cleaned rows; approximate
    problems: list[WorkspaceCleanupEntry]  # latest blocked or failed rows


class WorkspaceCleanupRunRequest(BaseModel):
    dry_run: bool = True  # a real run needs dry_run=false and the admin scope
    wait_s: float = Field(default=5.0, ge=0, le=30)
