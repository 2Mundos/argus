from datetime import timedelta
from uuid import uuid4

from argus_core import codex_auth
from argus_core.accounts import (
    Account,
    AccountCreate,
    AccountLease,
    AccountUpdate,
    CredentialUpdate,
    ExhaustedReport,
    FallbackResult,
    LeaseRequest,
    LeaseRole,
    UsageReport,
)
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from argus_api.accounts import service
from argus_api.db import utcnow
from argus_api.deps import (
    AdminDep,
    AgentDep,
    CallerDep,
    DbDep,
    ManagerDep,
    SecretsDep,
    SettingsDep,
)
from argus_api.errors import ForbiddenError, ValidationFailedError
from argus_api.secrets import SecretStoreError

router = APIRouter(prefix="/accounts", tags=["accounts"])


def check_openai_credential(provider: str, credential: dict | None) -> str | None:
    """An `openai` login must be a Codex auth.json (codex accounts spec 3). Returns
    the account kind it implies; errors name fields, never values."""
    if provider != codex_auth.PROVIDER or credential is None:
        return None
    try:
        return codex_auth.validate(credential).kind
    except codex_auth.CodexAuthError as exc:
        raise ValidationFailedError(f"invalid openai credential: {exc}") from None


@router.get("")
async def list_accounts(db: DbDep, _: ManagerDep) -> list[Account]:
    async with db.read() as conn:
        return await service.list_accounts(conn, utcnow())


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate, db: DbDep, caller: AdminDep, secrets: SecretsDep
) -> Account:
    """Register an account. A `credential` is written to the SOPS file first
    (as `account-<id>`); the registry row only ever holds the reference."""
    account_id = str(uuid4())
    if kind := check_openai_credential(body.provider, body.credential):
        body = body.model_copy(update={"kind": kind})
    secret_ref = body.secret_ref
    if body.credential is not None:
        name = f"account-{account_id}"
        await secrets.put(name, body.credential)
        secret_ref = f"sops:{name}"
    assert secret_ref is not None
    async with db.write() as tx:
        return await service.create_account(
            tx, account_id, body, secret_ref, caller.actor, secrets
        )


class CodexLoginStart(BaseModel):
    name: str | None = Field(default=None, max_length=100)


class CodexLogin(BaseModel):
    """A dedicated Codex login in progress. The code and link are shown only
    while `pending`, only to the token that started it."""

    login_id: str
    state: str
    verification_url: str | None
    user_code: str | None
    expires_in_s: int
    account_id: str | None
    error: str | None


@router.post("/codex-login", status_code=status.HTTP_201_CREATED)
async def start_codex_login(
    body: CodexLoginStart, request: Request, caller: AdminDep
) -> CodexLogin:
    """Start the official client's device-code login for a new `openai` account
    (codex accounts spec 3). On success the account appears in the registry."""
    manager = request.app.state.codex_login
    return CodexLogin(**await manager.start(caller.token_id, caller.actor, body.name))


@router.get("/codex-login/{login_id}")
async def get_codex_login(
    login_id: str, request: Request, caller: AdminDep
) -> CodexLogin:
    return CodexLogin(**request.app.state.codex_login.get(login_id, caller.token_id))


@router.delete("/codex-login/{login_id}")
async def cancel_codex_login(
    login_id: str, request: Request, caller: AdminDep
) -> CodexLogin:
    return CodexLogin(
        **await request.app.state.codex_login.cancel(login_id, caller.token_id)
    )


@router.get("/{account_id}")
async def get_account(account_id: str, db: DbDep, _: ManagerDep) -> Account:
    async with db.read() as conn:
        return await service.get_account(conn, account_id, utcnow())


@router.patch("/{account_id}")
async def update_account(
    account_id: str, body: AccountUpdate, db: DbDep, caller: AdminDep
) -> Account:
    async with db.write() as tx:
        return await service.update_account(tx, account_id, body, caller.actor)


@router.post("/lease")
async def lease_account(
    body: LeaseRequest, db: DbDep, agent_id: AgentDep, secrets: SecretsDep
) -> AccountLease:
    """Lease the best usable account among `providers` (most preferred first),
    with its credential. One lease per agent; it ends when the agent does.
    `409 no_account` when none is usable."""
    async with db.write() as tx:
        return await service.lease(tx, agent_id, body.providers, secrets, body.role)


@router.post("/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_account(
    db: DbDep, agent_id: AgentDep, role: LeaseRole | None = None
) -> None:
    """End the agent's leases: one `role`, or all of them."""
    async with db.write() as tx:
        await service.release(tx, agent_id, "released", role)


@router.post("/{account_id}/exhausted")
async def report_exhausted(
    account_id: str,
    body: ExhaustedReport,
    db: DbDep,
    agent_id: AgentDep,
    secrets: SecretsDep,
    settings: SettingsDep,
) -> FallbackResult:
    """Report that the leased account hit a quota. Returns the next account to use
    (same provider first), or none: then the given task is blocked as
    `no_account` and returns to ready when an account frees up."""
    async with db.write() as tx:
        return await service.exhausted(
            tx,
            agent_id,
            account_id,
            body,
            secrets,
            cooldown=timedelta(minutes=settings.account_cooldown_minutes),
        )


@router.post("/{account_id}/usage", status_code=status.HTTP_204_NO_CONTENT)
async def report_usage(
    account_id: str, body: UsageReport, db: DbDep, agent_id: AgentDep
) -> None:
    async with db.write() as tx:
        await service.record_usage(tx, account_id, agent_id, body)


@router.put("/{account_id}/credential", status_code=status.HTTP_204_NO_CONTENT)
async def update_credential(
    account_id: str,
    body: CredentialUpdate,
    db: DbDep,
    caller: CallerDep,
    secrets: SecretsDep,
) -> None:
    """Replace the credential (e.g. refreshed OAuth tokens). Allowed for the agent
    holding a lease on the account, or an admin. If it cannot be persisted, the
    account is disabled rather than left with a token that would be lost."""
    if caller.agent_id is None and caller.scope != "admin":
        raise ForbiddenError("requires the leasing agent or an admin")
    async with db.read() as conn:
        name = await service.secret_name_for_update(conn, account_id, caller.agent_id)
        account = await service.get_account(conn, account_id, utcnow())
    check_openai_credential(account.provider, body.credential)
    try:
        await secrets.put(name, body.credential)
    except SecretStoreError as exc:
        async with db.write() as tx:
            await service.disable_for_write_failure(tx, account_id, exc.message)
        raise
