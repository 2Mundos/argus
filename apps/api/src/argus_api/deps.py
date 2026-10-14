"""FastAPI dependencies: database access and scope checks (worker lifecycle 3.6)."""

from typing import Annotated

from argus_core.auth import Scope
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from argus_api.auth import Identity, authenticate
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.errors import ForbiddenError, UnauthorizedError
from argus_api.secrets import SecretStore

_bearer = HTTPBearer(auto_error=False)


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_secrets(request: Request) -> SecretStore:
    return request.app.state.secrets


async def get_caller(
    db: Annotated[Database, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Identity:
    """Every route except /healthz and /agents/register needs a valid token."""
    if credentials is None:
        raise UnauthorizedError("missing bearer token", code="missing_token")
    async with db.read() as conn:
        return await authenticate(conn, credentials.credentials)


def require(*scopes: Scope):
    def check(caller: Annotated[Identity, Depends(get_caller)]) -> Identity:
        if caller.scope not in scopes:
            raise ForbiddenError(
                f"requires scope {' or '.join(scopes)}; this token is {caller.scope}"
            )
        return caller

    return Depends(check)


def require_agent(caller: Annotated[Identity, Depends(get_caller)]) -> str:
    """Worker operations need an agent identity (worker or orchestrator)."""
    if caller.agent_id is None:
        raise ForbiddenError("requires an agent token; admin tokens are not agents")
    return caller.agent_id


DbDep = Annotated[Database, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SecretsDep = Annotated[SecretStore, Depends(get_secrets)]
CallerDep = Annotated[Identity, Depends(get_caller)]
AgentDep = Annotated[str, Depends(require_agent)]
ManagerDep = Annotated[Identity, require(Scope.ORCHESTRATOR, Scope.ADMIN)]
AdminDep = Annotated[Identity, require(Scope.ADMIN)]
