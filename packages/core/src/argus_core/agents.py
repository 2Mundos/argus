"""Agent identity and liveness models (worker lifecycle 3.4)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Role(StrEnum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"


class AgentState(StrEnum):
    """Stored state. Whether an active agent is busy or idle is derived from the
    tasks it holds, never stored, so it cannot drift."""

    ACTIVE = "active"
    DRAINING = "draining"  # finishes current work, claims nothing new
    GONE = "gone"  # left cleanly
    LOST = "lost"  # missed heartbeats; irreversible for this identity


LIVE = frozenset({AgentState.ACTIVE, AgentState.DRAINING})


class AgentRuntime(BaseModel):
    """What an agent says it runs, sent on heartbeat. Self-reported: it proves a
    live process said so, not that the engine or model works."""

    worker_version: str | None = Field(default=None, max_length=100)
    engine: str | None = Field(default=None, max_length=100)
    executor: str | None = Field(default=None, max_length=100)
    # Capabilities the worker will act on. `steer` means it injects operator steers.
    features: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("features")
    @classmethod
    def _features(cls, value: list[str]) -> list[str]:
        kept: list[str] = []
        for item in value:
            item = item.strip()
            if not item or len(item) > 40:
                raise ValueError("a feature name must be 1 to 40 characters")
            if item not in kept:
                kept.append(item)
        return kept


class Agent(BaseModel):
    id: str
    name: str
    role: Role
    instance_id: str | None
    capabilities: list[str]
    state: AgentState
    claimed_tasks: int
    last_heartbeat_at: datetime
    created_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    archived_at: datetime | None = None  # set while an operator has archived it
    runtime: AgentRuntime | None = None  # last reported on heartbeat


class BootstrapTokenCreate(BaseModel):
    role: Role = Role.WORKER
    instance_id: str | None = None
    ttl_s: int = Field(default=3600, ge=60, le=7 * 24 * 3600)


class AgentRegister(BaseModel):
    bootstrap_token: str
    name: str = Field(default="", max_length=100)
    capabilities: list[str] = []
