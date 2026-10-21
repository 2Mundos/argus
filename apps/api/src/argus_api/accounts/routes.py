from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from argus_core import claude_auth, codex_auth, cursor_auth, custom_accounts
from argus_core.accounts import (
    Account,
    AccountCreate,
    AccountLease,
    AccountUpdate,
    AwaitingQuotaRequest,
    AwaitingQuotaResult,
    CredentialUpdate,
    ExhaustedReport,
    FallbackResult,
    LeaseRequest,
    LeaseRole,
    QuotaRefresh,
    UsageReport,
    UsageSeries,
)
from argus_core.cost import CostReport, CostWindow
from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field

from argus_api.accounts import cost, custom, pricing, service, usage
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
from argus_api.secrets import SecretStoreError, ref_name
from argus_api.tasks import service as tasks

router = APIRouter(prefix="/accounts", tags=["accounts"])


def check_credential(provider: str, credential: dict | None) -> tuple[str, str] | None:
    """Validate a provider's login document; returns the (kind, mode) it implies,
    or None for providers whose documents are not checked. Errors name fields,
    never values."""
    if credential is None:
        return None
    try:
        if provider == claude_auth.PROVIDER:
            found = claude_auth.validate(credential)
        elif provider == codex_auth.PROVIDER:
            found = codex_auth.validate(credential)
        elif provider == cursor_auth.PROVIDER:
            found = cursor_auth.validate(credential)
        elif provider == custom_accounts.PROVIDER:
            custom_accounts.validate_credential(credential)
            return "api_key", "api_key"
        elif provider == "gitea":
            token = credential.get("token")
            username = credential.get("username")
            if (
                not isinstance(token, str)
                or not token.strip()
                or "\r" in token
                or "\n" in token
            ):
                raise ValidationFailedError(
                    "invalid gitea credential: required field token"
                )
            if (
                not isinstance(username, str)
                or not username.strip()
                or any(char in username for char in (":", "\r", "\n"))
            ):
                raise ValidationFailedError(
                    "invalid gitea credential: required valid field username"
                )
            return "api_key", "token"
        else:
            if not credential:
                raise ValidationFailedError(
                    f"invalid {provider} credential: must be a non-empty JSON object"
                )
            return None
    except (
        claude_auth.ClaudeAuthError,
        codex_auth.CodexAuthError,
        cursor_auth.CursorAuthError,
        custom_accounts.CustomAccountError,
    ) as exc:
        raise ValidationFailedError(f"invalid {provider} credential: {exc}") from None
    return found.kind, found.mode


@router.get("")
async def list_accounts(db: DbDep, _: ManagerDep) -> list[Account]:
    async with db.read() as conn:
        return await service.list_accounts(conn, utcnow())


@router.get("/usage")
async def account_usage(
    db: DbDep,
    _: ManagerDep,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime | None, Query(alias="to")] = None,
    bucket: Literal["hour", "day"] = "day",
    tz: str = "UTC",
    account_id: str | None = None,
) -> UsageSeries:
    """Reported tokens per account over [from, to), bucketed in the viewer's
    timezone (account usage 3.5). Buckets with no rows are measured zeros."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationFailedError(f"unknown timezone: {tz}") from None
    end = end or utcnow()
    if start.tzinfo is None or end.tzinfo is None:
        raise ValidationFailedError("from and to need a UTC offset")
    if start >= end:
        raise ValidationFailedError("from must be before to")
    async with db.read() as conn:
        if account_id is None:
            ids = [a.id for a in await service.list_accounts(conn, utcnow())]
        else:
            ids = [(await service.get_account(conn, account_id, utcnow())).id]
        accounts = await usage.series(conn, start, end, bucket, zone, ids)
    return UsageSeries(start=start, end=end, bucket=bucket, tz=tz, accounts=accounts)


@router.get("/cost")
async def account_cost(
    db: DbDep,
    _: ManagerDep,
    settings: SettingsDep,
    window: CostWindow = "24h",
    tz: str = "UTC",
) -> CostReport:
    """Tokens and estimated cost per account, project and day over the window
    ending now (cost view 3.3). Read only."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationFailedError(f"unknown timezone: {tz}") from None
    table = pricing.load(settings.pricing_file)
    async with db.read() as conn:
        return await cost.report(conn, utcnow(), window, tz, zone, table)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate, db: DbDep, caller: AdminDep, secrets: SecretsDep
) -> Account:
    """Register an account. A `credential` is written to the SOPS file first
    (as `account-<id>`); the registry row only ever holds the reference."""
    account_id = str(uuid4())
    if body.provider == "gitea" and body.credential is None:
        raise ValidationFailedError(
            "invalid gitea credential: username and token must be validated at registration"
        )
    if body.provider == custom_accounts.PROVIDER and body.credential is None:
        raise ValidationFailedError(
            "invalid custom credential: give the api_key as credential"
        )
    if checked := check_credential(body.provider, body.credential):
        body = body.model_copy(update={"kind": checked[0]})
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


class ClaudeLoginStart(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    renew_account_id: str | None = Field(default=None, max_length=64)


class ClaudeLoginCode(BaseModel):
    """The code the sign-in page shows. Sensitive: never echoed or logged."""

    code: str


class ClaudeLogin(BaseModel):
    """A dedicated Claude login in progress. The link is shown only while
    `pending`, only to the token that started it."""

    login_id: str
    state: str
    phase: str | None
    attempts: int
    max_attempts: int
    refusal: str | None
    url: str | None
    expires_in_s: int
    account_id: str | None
    error: str | None


@router.post("/claude-login", status_code=status.HTTP_201_CREATED)
async def start_claude_login(
    body: ClaudeLoginStart, request: Request, caller: AdminDep
) -> ClaudeLogin:
    """Start the official client's `setup-token` login for a new `anthropic`
    account, or to renew `renew_account_id` (claude accounts spec 3)."""
    manager = request.app.state.claude_login
    return ClaudeLogin(
        **await manager.start(
            caller.token_id, caller.actor, body.name, body.renew_account_id
        )
    )


@router.get("/claude-login/{login_id}")
async def get_claude_login(
    login_id: str, request: Request, caller: AdminDep
) -> ClaudeLogin:
    return ClaudeLogin(**request.app.state.claude_login.get(login_id, caller.token_id))


@router.post("/claude-login/{login_id}/code")
async def send_claude_login_code(
    login_id: str, body: ClaudeLoginCode, request: Request, caller: AdminDep
) -> ClaudeLogin:
    """Hand the sign-in page's code to the waiting client, once."""
    return ClaudeLogin(
        **await request.app.state.claude_login.submit_code(
            login_id, caller.token_id, body.code
        )
    )


@router.delete("/claude-login/{login_id}")
async def cancel_claude_login(
    login_id: str, request: Request, caller: AdminDep
) -> ClaudeLogin:
    return ClaudeLogin(
        **await request.app.state.claude_login.cancel(login_id, caller.token_id)
    )


@router.get("/{account_id}")
async def get_account(account_id: str, db: DbDep, _: ManagerDep) -> Account:
    async with db.read() as conn:
        return await service.get_account(conn, account_id, utcnow())


@router.post("/{account_id}/quota/refresh")
async def refresh_quota(
    account_id: str, request: Request, db: DbDep, _: ManagerDep
) -> QuotaRefresh:
    """Read the provider's quota now (Codex only; account usage 3.3). At most once
    per account per 5 minutes; never refreshes the credential."""
    async with db.read() as conn:
        await service.get_account(conn, account_id, utcnow())
    return await request.app.state.quota.refresh(account_id)


async def _custom_account(
    request: Request, db: DbDep, account_id: str
) -> tuple[custom_accounts.CustomConfig, str | None]:
    """The account's config and (if stored) key; 422 for any other provider."""
    async with db.read() as conn:
        account = await service.get_account(conn, account_id, utcnow())
    if account.config is None:
        raise ValidationFailedError("not a custom account")
    name = ref_name(account.secret_ref)
    stored = request.app.state.secrets.get(name) if name else None
    key = stored.get("api_key") if isinstance(stored, dict) else None
    return account.config, key if isinstance(key, str) else None


@router.post("/{account_id}/probe")
async def probe_custom_account(
    account_id: str, request: Request, db: DbDep, _: AdminDep
) -> custom.CustomProbe:
    """Readiness of a custom account: `GET {base_url}/models` with its key (custom
    accounts spec 3). Returns the model ids as suggestions; never the key, and
    only a fixed reason, never upstream text."""
    config, key = await _custom_account(request, db, account_id)
    if key is None:
        return custom.CustomProbe(status="failed", reason="credential not found")
    return await custom.probe(
        config, key, getattr(request.app.state, "custom_transport", None)
    )


@router.get("/{account_id}/models/{author}/{slug}/endpoints")
async def custom_model_endpoints(
    account_id: str,
    author: str,
    slug: str,
    request: Request,
    db: DbDep,
    _: ManagerDep,
) -> custom_accounts.ModelEndpoints:
    """The hosts serving an OpenRouter model, with quantization, context and price,
    for the routing pickers (custom accounts spec 3). Proxied; `author` and `slug`
    are validated as one path segment each."""
    config, _key = await _custom_account(request, db, account_id)
    return await custom.model_endpoints(
        config, author, slug, getattr(request.app.state, "custom_transport", None)
    )


@router.patch("/{account_id}")
async def update_account(
    account_id: str, body: AccountUpdate, db: DbDep, caller: AdminDep
) -> Account:
    async with db.write() as tx:
        return await service.update_account(tx, account_id, body, caller.actor)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: str,
    confirm_name: str,
    db: DbDep,
    caller: AdminDep,
    secrets: SecretsDep,
) -> None:
    """Delete an account and, if nothing else references it, its Argus-generated
    credential. `confirm_name` must equal the current name; `409 account_in_use`
    while a lease is active.

    SOPS and SQLite cannot commit together. The secret is removed inside the SQL
    transaction, so a store failure rolls the rows back; if the commit then fails
    after the secret is gone, the exact prior value is written back (compensation,
    best effort: if that write also fails the error names the entry, never a value)."""
    removed: str | None = None
    prior: dict | None = None
    try:
        async with db.write() as tx:
            removed = await service.delete_account(
                tx, account_id, confirm_name, caller.actor
            )
            if removed is not None:
                prior = secrets.get(removed)
                await secrets.delete(removed)
    except BaseException:
        if removed is not None and prior is not None and secrets.get(removed) is None:
            try:
                await secrets.put(removed, prior)
            except SecretStoreError:
                raise SecretStoreError(
                    f"account delete failed and secret {removed!r} could not be"
                    " restored; recover it from the secrets file backup",
                    code="secret_restore_failed",
                ) from None
        raise


@router.post("/lease")
async def lease_account(
    body: LeaseRequest, db: DbDep, agent_id: AgentDep, secrets: SecretsDep
) -> AccountLease:
    """Lease the best usable account and deliver its credential to a claimed task.

    `runtime_providers` is required and states what the runtime can apply. The
    caller must hold the live claim identified by `task_id` and `attempt`. One
    active lease per role; `409 no_account` when none is usable."""
    async with db.write() as tx:
        return await service.lease(
            tx,
            agent_id,
            body.runtime_providers,
            secrets,
            body.role,
            reuse_held=body.reuse_held,
            task_id=body.task_id,
            attempt=body.attempt,
        )


@router.post("/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_account(
    db: DbDep, agent_id: AgentDep, role: LeaseRole | None = None
) -> None:
    """End the agent's leases: one `role`, or all of them."""
    async with db.write() as tx:
        await service.release(tx, agent_id, "released", role)


@router.post("/awaiting-quota")
async def awaiting_quota(
    body: AwaitingQuotaRequest, db: DbDep, agent_id: AgentDep
) -> AwaitingQuotaResult:
    """The agent could not lease an account and so cannot claim: park the ready tasks
    it could have taken as `awaiting_quota`. `409 account_available` if one is usable."""
    async with db.write() as tx:
        ids = await service.mark_awaiting_quota(
            tx, agent_id, body.capabilities, body.runtime_providers
        )
        return AwaitingQuotaResult(
            task_ids=ids,
            refill_at=await service.earliest_reset(
                tx, "engine", body.runtime_providers
            ),
        )


@router.post("/{account_id}/exhausted")
async def report_exhausted(
    account_id: str,
    body: ExhaustedReport,
    db: DbDep,
    agent_id: AgentDep,
    secrets: SecretsDep,
    settings: SettingsDep,
) -> FallbackResult:
    """Mark a leased account exhausted and return fallback metadata only.

    The caller must hold the live claim identified by `task_id` and `attempt`;
    `runtime_providers` controls which fallback metadata may be returned. Obtain
    any next credential through `/accounts/lease`."""
    if body.task_id is None or body.attempt is None:
        raise ValidationFailedError("task_id and attempt are required")
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
    account is disabled rather than left with a token that would be lost.

    An agent must also hold the live task claim named by `task_id` and `attempt`."""
    if caller.agent_id is None and caller.scope != "admin":
        raise ForbiddenError("requires the leasing agent or an admin")
    async with db.read() as conn:
        name = await service.secret_name_for_update(conn, account_id, caller.agent_id)
        account = await service.get_account(conn, account_id, utcnow())
    if caller.agent_id is not None:
        if body.task_id is None or body.attempt is None:
            raise ValidationFailedError("task_id and attempt are required")
        async with db.write() as tx:
            await tasks.check_lease(tx, body.task_id, body.attempt, caller.agent_id)
    checked = check_credential(account.provider, body.credential)
    if caller.agent_id is not None:
        prior = secrets.get(name)
        if prior is None or _credential_shape(prior) != _credential_shape(
            body.credential
        ):
            raise ValidationFailedError(
                "an agent may rotate only the same credential document shape"
            )
        if account.provider == "gitea" and prior.get("username") != body.credential.get(
            "username"
        ):
            raise ValidationFailedError("gitea username cannot change during rotation")
        if checked is not None and checked[0] != account.kind:
            raise ValidationFailedError(
                "credential kind cannot change during agent rotation"
            )
    try:
        await secrets.put(name, body.credential)
    except SecretStoreError as exc:
        async with db.write() as tx:
            await service.disable_for_write_failure(tx, account_id, exc.message)
        raise
    if checked and caller.agent_id is None:
        # An admin renewal: the kind follows the credential, nothing else changes.
        async with db.write() as tx:
            await service.credential_renewed(tx, account_id, *checked, caller.actor)


def _credential_shape(value: object) -> object:
    """Describe nested key structure and JSON value types without retaining values."""
    if isinstance(value, dict):
        return {key: _credential_shape(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return [_credential_shape(child) for child in value]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__
