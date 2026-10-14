"""FastAPI dependencies shared by routers."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from argus_api.db import Database


def get_db(request: Request) -> Database:
    return request.app.state.db


@dataclass(frozen=True)
class Caller:
    actor: str
    agent_id: str | None


def get_caller(
    x_argus_agent: Annotated[str | None, Header()] = None,
) -> Caller:
    """TEMPORARY identity until token auth (worker lifecycle 3.6, build step 4).

    Agents identify themselves with the `X-Argus-Agent` header; anyone else is an
    anonymous human. Nothing here is authenticated yet.
    """
    if x_argus_agent:
        return Caller(actor=f"agent:{x_argus_agent}", agent_id=x_argus_agent)
    return Caller(actor="human:anonymous", agent_id=None)


def require_agent(caller: Annotated[Caller, Depends(get_caller)]) -> str:
    if caller.agent_id is None:
        raise HTTPException(401, "this operation requires an agent identity")
    return caller.agent_id


DbDep = Annotated[Database, Depends(get_db)]
CallerDep = Annotated[Caller, Depends(get_caller)]
AgentDep = Annotated[str, Depends(require_agent)]
