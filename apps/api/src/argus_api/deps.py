"""FastAPI dependencies shared by routers."""

from dataclasses import dataclass
from typing import Annotated

from argus_core.agents import Role
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from argus_api.agents import service as agents
from argus_api.db import Database

_bearer = HTTPBearer(auto_error=False)


def get_db(request: Request) -> Database:
    return request.app.state.db


@dataclass(frozen=True)
class Caller:
    actor: str
    agent_id: str | None = None
    role: Role | None = None


async def get_caller(
    db: Annotated[Database, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    """An agent authenticated by its bearer token, or an anonymous human.

    Anonymous human access is TEMPORARY until admin auth and scopes
    (worker lifecycle 3.6, build step 4). An invalid token is always a 401,
    never a silent downgrade to anonymous.
    """
    if credentials is None:
        return Caller(actor="human:anonymous")
    async with db.read() as conn:
        identity = await agents.authenticate(conn, credentials.credentials)
    return Caller(
        actor=f"agent:{identity.agent_id}",
        agent_id=identity.agent_id,
        role=identity.role,
    )


def require_agent(caller: Annotated[Caller, Depends(get_caller)]) -> str:
    if caller.agent_id is None:
        raise HTTPException(401, "this operation requires an agent token")
    return caller.agent_id


DbDep = Annotated[Database, Depends(get_db)]
CallerDep = Annotated[Caller, Depends(get_caller)]
AgentDep = Annotated[str, Depends(require_agent)]
