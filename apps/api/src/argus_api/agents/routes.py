from collections import Counter
from datetime import datetime

from argus_core.agents import (
    Agent,
    AgentRegister,
    AgentRuntime,
    AgentState,
    BootstrapTokenCreate,
)
from argus_core.model_trace import Execution, LayerModels
from fastapi import APIRouter, status
from pydantic import BaseModel

from argus_api.agents import service
from argus_api.deps import AdminDep, AgentDep, CallerDep, DbDep, ManagerDep, SettingsDep
from argus_api.modeltrace import service as modeltrace

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


class HeartbeatRequest(BaseModel):
    leases: list[LeaseRef] = []
    runtime: AgentRuntime | None = None  # what you run; shown as validated


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
        reg = await service.register(
            tx, body, token_ttl_s=settings.agent_token_ttl_s
        )
    return Registration(
        agent=reg.agent,
        token=reg.token,
        heartbeat_interval_s=service.HEARTBEAT_INTERVAL_S,
    )


@router.get("")
async def list_agents(
    db: DbDep, _: CallerDep, state: AgentState | None = None
) -> list[Agent]:
    async with db.read() as conn:
        return await service.list_agents(conn, state=state)


@router.post("/me/heartbeat")
async def heartbeat(
    body: HeartbeatRequest, db: DbDep, agent_id: AgentDep, settings: SettingsDep
) -> HeartbeatResponse:
    """Prove liveness and renew the listed task leases (each one independently)."""
    async with db.write() as tx:
        hb = await service.heartbeat(
            tx,
            agent_id,
            [(lease.task_id, lease.attempt) for lease in body.leases],
            body.runtime,
            token_ttl_s=settings.agent_token_ttl_s,
        )
    return HeartbeatResponse(
        state=hb.state,
        renewed=[RenewedLease(**vars(r)) for r in hb.renewed],
        rejected=[RejectedLease(**vars(r)) for r in hb.rejected],
        heartbeat_interval_s=service.HEARTBEAT_INTERVAL_S,
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
