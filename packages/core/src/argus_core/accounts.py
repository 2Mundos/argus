"""Provider account pool (worker lifecycle 3.9)."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

LeaseRole = Literal["engine", "executor", "repository"]

# Technical facts, not operator policy (account selection spec 3): the account
# provider of repository access, and the one provider that only a delegated CLI
# (never a NanoClaw harness) can run on.
REPOSITORY_PROVIDER = "gitea"
EXECUTOR_ONLY = frozenset({"cursor"})


def role_accepts(
    role: LeaseRole, provider: str, runtime_providers: list[str] | None = None
) -> bool:
    """May an account of `provider` be leased for `role`? Repository accounts serve
    only the repository role and never become model accounts; a harness cannot run
    on an executor-only provider. `runtime_providers` is what the leasing runtime
    reports it can technically apply (None: no statement), never a preference."""
    if role == "repository":
        return provider == REPOSITORY_PROVIDER
    if provider == REPOSITORY_PROVIDER:
        return False
    if role == "engine" and provider in EXECUTOR_ONLY:
        return False
    return runtime_providers is None or provider in runtime_providers


class AccountKind(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


class AccountState(StrEnum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"  # until exhausted_until, then available again
    DISABLED = "disabled"


QuotaPeriod = Literal["5h", "24h", "7d"]
PERIOD_HOURS: dict[str, int] = {"5h": 5, "24h": 24, "7d": 168}


def _check_token_limits(v: dict[str, int] | None) -> dict[str, int] | None:
    if v is None:
        return v
    for period, limit in v.items():
        if period not in PERIOD_HOURS:
            raise ValueError(f"unknown token limit window {period!r}: use 5h, 24h, 7d")
        if limit < 1:
            raise ValueError(f"token limit for {period} must be a positive integer")
    return v


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_-]+$")
    kind: AccountKind = AccountKind.API_KEY
    priority: int = 0
    max_leases: int | None = Field(default=None, ge=1)  # None: unlimited
    labels: dict[str, str] = {}
    # Exactly one: the credential itself (stored encrypted in the SOPS file), or a
    # reference to an entry that is already there (`sops:<name>`).
    credential: dict[str, Any] | None = None
    secret_ref: str | None = Field(default=None, pattern=r"^sops:[A-Za-z0-9_-]{1,128}$")
    # Max total (input + output) tokens per rolling window; used to estimate quota.
    token_limits: dict[str, int] | None = None

    _limits = field_validator("token_limits")(_check_token_limits)

    @model_validator(mode="after")
    def one_secret_source(self) -> "AccountCreate":
        if (self.credential is None) == (self.secret_ref is None):
            raise ValueError("give exactly one of credential or secret_ref")
        return self


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    priority: int | None = None
    max_leases: int | None = Field(default=None, ge=1)
    labels: dict[str, str] | None = None
    enabled: bool | None = None
    billing_day: int | None = Field(default=None, ge=1, le=31)
    token_limits: dict[str, int] | None = None  # null clears

    _limits = field_validator("token_limits")(_check_token_limits)


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cost_usd: float
    runs: int = 0  # usage reports counted (every row)
    unmeasured_runs: int = 0  # of those, runs whose engine reported no figures


QuotaSource = Literal["reported", "polled", "estimated"]


class QuotaReport(BaseModel):
    """One quota window as a provider stated it (account usage 3.1): a timed
    window with a percentage, or a two-state pool (exhausted or not)."""

    window_minutes: int | None = Field(default=None, gt=0)
    pool: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,40}$")
    used_percent: float | None = Field(default=None, ge=0, le=100)
    resets_at: datetime | None = None
    exhausted_until: datetime | None = None

    @model_validator(mode="after")
    def one_window(self) -> "QuotaReport":
        if (self.window_minutes is None) == (self.pool is None):
            raise ValueError("give exactly one of window_minutes or pool")
        return self

    @property
    def window(self) -> str:
        return self.pool or f"{self.window_minutes}m"


class QuotaWindow(BaseModel):
    window: str
    window_minutes: int | None
    used_percent: float | None
    resets_at: datetime | None
    exhausted_until: datetime | None
    observed_at: datetime
    source: QuotaSource


class QuotaStatusWindow(BaseModel):
    period: QuotaPeriod
    limit: int
    used: int
    remaining: int
    used_percent: float
    source: Literal["estimated"] = "estimated"


class QuotaStatus(BaseModel):
    """Whether Argus knows this account's quota, and if not, why (never a 0/100)."""

    state: Literal["known", "unknown"]
    reason: (
        Literal["no_usage_records", "provider_does_not_expose", "window_not_configured"]
        | None
    ) = None
    windows: list[QuotaStatusWindow] = []


class Account(BaseModel):
    """Registry entry. Never contains the credential."""

    id: str
    name: str
    provider: str
    kind: AccountKind
    secret_ref: str
    priority: int
    max_leases: int | None
    state: AccountState
    exhausted_until: datetime | None
    disabled_reason: str | None
    labels: dict[str, str]
    active_leases: int
    usage_24h: Usage
    usage_total: Usage
    quota: list[QuotaWindow] = []
    quota_status: QuotaStatus
    token_limits: dict[str, int] | None = None
    billing_day: int | None = None
    created_at: datetime
    updated_at: datetime


class LeaseRequest(BaseModel):
    """Lease the best usable account globally (priority, then least leased).

    The retired `providers` list is accepted and ignored: no caller-side order or
    allowlist restricts selection."""

    # Providers the runtime can technically apply (it knows how to inject them);
    # absent means no statement. Capability, not preference: it never orders.
    runtime_providers: list[str] | None = Field(default=None, min_length=1)
    # `engine`: the account the worker's harness runs on; `executor`: the one a
    # CLI it delegates to uses. One active lease per role.
    role: LeaseRole = "engine"
    # Executor role only: when the best account is one this agent already holds as
    # its engine, return that lease unchanged and take no extra slot (a delegated
    # CLI of the same provider runs on the engine's own credential). Otherwise the
    # executor lease is an ordinary separate one.
    reuse_held: bool = False


class AccountLease(BaseModel):
    """What a leasing agent receives, including the credential to use."""

    lease_id: str
    account_id: str
    name: str
    provider: str
    kind: AccountKind
    role: LeaseRole = "engine"
    credential: dict[str, Any]


class ExhaustedReport(BaseModel):
    until: datetime | None = None  # the provider's reset time, if known
    reason: str = Field(default="", max_length=2000)
    runtime_providers: list[str] | None = Field(default=None, min_length=1)
    # Which of the agent's leases hit the limit (it may hold one account twice).
    role: LeaseRole = "engine"
    # The task the agent was working on: blocked as `awaiting_quota` if nothing is left.
    task_id: str | None = None
    attempt: int | None = None


class AwaitingQuotaRequest(BaseModel):
    """An agent with no usable account asks for the work it cannot claim to wait."""

    capabilities: list[str] = []
    runtime_providers: list[str] | None = Field(default=None, min_length=1)


class AwaitingQuotaResult(BaseModel):
    task_ids: list[str]
    refill_at: datetime | None = None  # earliest exhausted_until


class FallbackResult(BaseModel):
    lease: AccountLease | None
    earliest_reset: datetime | None = None
    task_blocked: bool = False


class UsageReport(BaseModel):
    """One engine run on one held account (account usage 3.2)."""

    task_id: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=100)
    engine: str | None = Field(default=None, max_length=50)
    role: Literal["engine", "executor"] | None = None
    rate_limits: list[QuotaReport] = Field(default=[], max_length=10)
    # Idempotency: built by the client from task+agent+engine+start. A repeat adds no row.
    run_key: str | None = Field(default=None, min_length=1, max_length=200)
    # not_reported: the run happened but the engine gave no figures (all null).
    measurement: Literal["measured", "not_reported"] = "measured"

    @model_validator(mode="after")
    def honest_figures(self) -> "UsageReport":
        figures = (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.cost_usd,
        )
        if self.measurement == "not_reported" and any(f is not None for f in figures):
            raise ValueError("a not_reported run must not carry tokens or cost")
        if self.measurement == "measured" and all(f is None for f in figures):
            raise ValueError(
                "a measured run needs tokens or cost; use measurement=not_reported"
            )
        return self


class UsageBucket(BaseModel):
    start: datetime  # in the requested timezone
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    runs: int
    unmeasured_runs: int = 0


class AccountUsage(BaseModel):
    account_id: str
    reported: bool  # has this account ever reported usage
    totals: Usage
    buckets: list[UsageBucket]


class UsageSeries(BaseModel):
    start: datetime
    end: datetime
    bucket: Literal["hour", "day"]
    tz: str
    accounts: list[AccountUsage]


class QuotaRefresh(BaseModel):
    status: Literal["updated", "skipped", "rate_limited", "failed"]
    reason: str | None = None
    quota: list[QuotaWindow]


class CredentialUpdate(BaseModel):
    """E.g. refreshed OAuth tokens for a subscription account."""

    credential: dict[str, Any]
