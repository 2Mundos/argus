from collections import Counter
from datetime import datetime
from typing import Annotated

from argus_core.agents import (
    Agent,
    AgentRegister,
    AgentRuntime,
    AgentState,
    BootstrapTokenCreate,
)
from argus_core.model_trace import Execution, LayerModels
from fastapi import APIRouter, status
from pydantic import BaseModel, Field, StringConstraints

from argus_api.agents import service
from argus_api.agents.hygiene import AgentHygiene, agent_hygiene
from argus_api.db import utcnow
from argus_api.deps import AdminDep, AgentDep, CallerDep, DbDep, ManagerDep, SettingsDep
from argus_api.errors import ConflictError
from argus_api.modeltrace import service as modeltrace
from argus_api.tasks.steers import SteerNotice

router = APIRouter(prefix="/agents", tags=["agents"])


class IssuedToken(BaseModel):
    token: str
    expires_at: datetime


class Registration(BaseModel):
    agent: Agent
    token: str
    heartbeat_interval_s: int


class LeaseRef(BaseModel):
    task_id: str
    attempt: int
    # Newest session/progress/log signal the worker saw for this lease (optional).
    activity_at: datetime | None = None


class HeartbeatRequest(BaseModel):
    leases: list[LeaseRef] = []
    runtime: AgentRuntime | None = None  # what you run; shown as validated
    # Steers this worker has injected since the last heartbeat (task steering spec).
    steer_acks: list[Annotated[str, StringConstraints(min_length=1, max_length=80)]] = (
        Field(default_factory=list, max_length=100)
    )


class RenewedLease(BaseModel):
    task_id: str
    attempt: int
    lease_expires_at: datetime


class RejectedLease(BaseModel):
    task_id: str
    attempt: int
    code: str
    message: str


class HeartbeatResponse(BaseModel):
    state: AgentState
    renewed: list[RenewedLease]
    rejected: list[RejectedLease]
    heartbeat_interval_s: int
    steers: list[SteerNotice] = []


@router.post("/bootstrap-tokens", status_code=status.HTTP_201_CREATED)
async def create_bootstrap_token(
    body: BootstrapTokenCreate, db: DbDep, caller: AdminDep
) -> IssuedToken:
    """Mint a one-time token an agent redeems at registration."""
    async with db.write() as tx:
        issued = await service.create_bootstrap_token(tx, body, caller.actor)
    return IssuedToken(token=issued.token, expires_at=issued.expires_at)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: AgentRegister, db: DbDep, settings: SettingsDep
) -> Registration:
    async with db.write() as tx:
        reg = await service.register(tx, body, token_ttl_s=settings.agent_token_ttl_s)
    return Registration(
        agent=reg.agent,
        token=reg.token,
        heartbeat_interval_s=service.HEARTBEAT_INTERVAL_S,
    )


@router.get("")
async def list_agents(
    db: DbDep,
    _: CallerDep,
    state: AgentState | None = None,
    archived: bool = False,
) -> list[Agent]:
    async with db.read() as conn:
        return await service.list_agents(conn, state=state, archived=archived)


class BulkArchiveRequest(BaseModel):
    """Which ended identities to archive: all of `states` (lost/gone), older than
    `older_than_days`. `dry_run` only counts. At most `limit` rows per call."""

    states: list[AgentState] = Field(min_length=1)
    older_than_days: int | None = Field(default=None, ge=0)
    limit: int = Field(default=200, ge=1, le=1000)
    dry_run: bool = False


class BulkArchiveResult(BaseModel):
    count: int
    dry_run: bool


@router.get("/hygiene")
async def get_agent_hygiene(
    db: DbDep, _: ManagerDep, settings: SettingsDep
) -> AgentHygiene:
    """Lost/gone aggregates by pool and age, claim holders, and the lost spike."""
    async with db.read() as conn:
        return await agent_hygiene(conn, now=utcnow(), settings=settings)


@router.post("/archive")
async def archive_agents(
    body: BulkArchiveRequest, db: DbDep, caller: AdminDep, settings: SettingsDep
) -> BulkArchiveResult:
    """Archive lost/gone identities older than N days, in a bounded batch.
    With no body `older_than_days` and no `ARGUS_AGENT_ARCHIVE_AFTER_DAYS`,
    answers 409 `agent_archive_disabled`. Non-lost/gone states are refused with
    422 `state_not_archivable` before anything changes."""
    days = (
        body.older_than_days
        if body.older_than_days is not None
        else settings.agent_archive_after_days
    )
    if days is None:
        raise ConflictError(
            "agent archive is disabled: set older_than_days or"
            " ARGUS_AGENT_ARCHIVE_AFTER_DAYS",
            code="agent_archive_disabled",
        )
    async with db.write() as tx:
        count = await service.archive_matching(
            tx,
            caller.actor,
            states=body.states,
            older_than_days=days,
            limit=body.limit,
            dry_run=body.dry_run,
        )
    return BulkArchiveResult(count=count, dry_run=body.dry_run)


@router.post("/me/heartbeat")
async def heartbeat(
    body: HeartbeatRequest, db: DbDep, agent_id: AgentDep, settings: SettingsDep
) -> HeartbeatResponse:
    """Prove liveness and renew the listed task leases (each one independently)."""
    async with db.write() as tx:
        hb = await service.heartbeat(
            tx,
            agent_id,
            [
                (lease.task_id, lease.attempt, lease.activity_at)
                for lease in body.leases
            ],
            body.runtime,
            body.steer_acks,
            token_ttl_s=settings.agent_token_ttl_s,
        )
    return HeartbeatResponse(
        state=hb.state,
        renewed=[RenewedLease(**vars(r)) for r in hb.renewed],
        rejected=[RejectedLease(**vars(r)) for r in hb.rejected],
        heartbeat_interval_s=service.HEARTBEAT_INTERVAL_S,
        steers=hb.steers,
    )


@router.post("/me/leave")
async def leave(db: DbDep, agent_id: AgentDep) -> Agent:
    """Exit cleanly: the identity ends and its token is revoked."""
    async with db.write() as tx:
        return await service.leave(tx, agent_id)


@router.get("/{agent_id}")
async def get_agent(agent_id: str, db: DbDep, _: CallerDep) -> Agent:
    async with db.read() as conn:
        return await service.get_agent(conn, agent_id)


class AgentModels(BaseModel):
    """What the worker ran lately: its latest run per layer and its history, each
    with its evidence. `current` is the latest recorded run, not a live claim."""

    agent_id: str
    current: dict[str, LayerModels]
    history: list[Execution]


@router.get("/{agent_id}/models")
async def get_agent_models(agent_id: str, db: DbDep, _: CallerDep) -> AgentModels:
    async with db.read() as conn:
        await service.get_agent(conn, agent_id)
        history = await modeltrace.agent_history(conn, agent_id)
    counts = Counter(run.layer for run in history)
    current: dict[str, LayerModels] = {}
    for run in reversed(history):  # oldest first: the last one per layer wins
        current[run.layer] = modeltrace.layer_models(run, counts[run.layer])
    return AgentModels(agent_id=agent_id, current=current, history=history)


@router.post("/{agent_id}/drain")
async def drain(agent_id: str, db: DbDep, caller: ManagerDep) -> Agent:
    """Let the agent finish its current work, then claim nothing new."""
    async with db.write() as tx:
        return await service.drain(tx, agent_id, caller.actor)
