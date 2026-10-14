from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from argus_api import auth
from argus_api.accounts import service as accounts
from argus_api.deps import AdminDep, DbDep, SecretsDep

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminTokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)


class AdminTokenIssued(BaseModel):
    id: str
    token: str


class AdminToken(BaseModel):
    id: str
    label: str | None
    created_by: str
    created_at: datetime
    revoked_at: datetime | None


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_admin_token(
    body: AdminTokenCreate, db: DbDep, caller: AdminDep
) -> AdminTokenIssued:
    """Mint another admin token. The secret is shown only in this response."""
    async with db.write() as tx:
        token_id, token = await auth.create_admin_token(tx, body.label, caller.actor)
    return AdminTokenIssued(id=token_id, token=token)


@router.get("/tokens")
async def list_admin_tokens(db: DbDep, _: AdminDep) -> list[AdminToken]:
    async with db.read() as conn:
        return [AdminToken(**vars(t)) for t in await auth.list_admin_tokens(conn)]


@router.post("/tokens/{token_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_admin_token(token_id: str, db: DbDep, _: AdminDep) -> None:
    async with db.write() as tx:
        await auth.revoke_admin_token(tx, token_id)


class SecretsReloaded(BaseModel):
    secrets: int


@router.post("/secrets/reload")
async def reload_secrets(
    db: DbDep, _: AdminDep, secrets: SecretsDep
) -> SecretsReloaded:
    """Re-read the SOPS file after editing it by hand (`sops secrets.sops.yaml`),
    then re-check every account's secret reference."""
    await secrets.reload()
    async with db.write() as tx:
        await accounts.reconcile_secrets(tx, secrets)
    return SecretsReloaded(secrets=len(secrets.names()))
