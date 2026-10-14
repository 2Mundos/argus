"""Agent identity and liveness models (worker lifecycle 3.4)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


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


class BootstrapTokenCreate(BaseModel):
    role: Role = Role.WORKER
    instance_id: str | None = None
    ttl_s: int = Field(default=3600, ge=60, le=7 * 24 * 3600)


class AgentRegister(BaseModel):
    bootstrap_token: str
    name: str = Field(default="", max_length=100)
    capabilities: list[str] = []
