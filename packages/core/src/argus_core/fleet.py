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

    @model_validator(mode="after")
    def desired_within_cap(self) -> "PoolUpsert":
        if self.desired_replicas > self.max_replicas:
            raise ValueError("desired_replicas cannot exceed max_replicas")
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
