"""Provider account pool (worker lifecycle 3.9)."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from argus_core.custom_accounts import PROVIDER as CUSTOM_PROVIDER
from argus_core.custom_accounts import CustomConfig, LeaseSettings

LeaseRole = Literal["engine", "executor", "repository"]

# Technical facts, not operator policy (account selection spec 3): the account
# provider of repository access, and the one provider that only a delegated CLI
# (never a NanoClaw harness) can run on.
REPOSITORY_PROVIDER = "gitea"
EXECUTOR_ONLY = frozenset({"cursor"})
# The node's own model, served on loopback (local harness spec): a harness, never
# a delegated CLI, holding no credential and no quota of its own.
LOCAL_PROVIDER = "local"
ENGINE_ONLY = frozenset({LOCAL_PROVIDER})
# Account providers a harness (the `engine` role) can run on: what a task may be
# restricted to. Never the repository provider or the executor-only ones.
ENGINE_PROVIDERS = frozenset({"anthropic", "openai", CUSTOM_PROVIDER, LOCAL_PROVIDER})


def role_accepts(
    role: LeaseRole, provider: str, runtime_providers: list[str] | None = None
) -> bool:
    """May an account of `provider` be leased for `role`? Repository accounts serve
    only the repository role and never become model accounts; a harness cannot run
    on an executor-only provider, and an engine-only one runs no delegated CLI.
    `runtime_providers` is what the leasing runtime reports it can technically
    apply. Credential-bearing leases require an explicit non-empty provider list."""
    if runtime_providers is None or provider not in runtime_providers:
        return False
    if role == "repository":
        return provider == REPOSITORY_PROVIDER
    if provider == REPOSITORY_PROVIDER:
        return False
    if role == "engine" and provider in EXECUTOR_ONLY:
        return False
    return not (role != "engine" and provider in ENGINE_ONLY)


def fallback_tier(provider: str, *, prefer_local: bool = False) -> int:
    """Lease tier, ahead of priority: every cloud account (0) outranks the local
    model (1), whatever the operator set, so the node's own GPU serves only when
    nothing in the cloud can (local harness spec 3). A trivial task inverts it for
    itself: local first, the cloud as the immediate fallback (task complexity spec)."""
    is_local = provider == LOCAL_PROVIDER
    return int(is_local != prefer_local)


def quota_exempt(provider: str) -> bool:
    """The local model has no provider quota, so it is never marked exhausted; it
    is unavailable only while its probe fails."""
    return provider == LOCAL_PROVIDER


# A local account's whole credential: where the model answers, the placeholder the
# harness sends instead of a token, the model name it configures and, optionally,
# the model's context window in tokens (`context_tokens`; when absent the lease
# carries the window the local model's probe measured). The harness compacts well
# before that window (local harness spec, "Harness orchestrates only").
LOCAL_CREDENTIAL_KEYS = frozenset({"base_url", "auth_token", "model", "context_tokens"})
LOCAL_CONTEXT_TOKENS_RANGE = (4096, 4_000_000)
LOCAL_AUTH_TOKEN = "argus-local-harness"  # noqa: S105 — public placeholder, not a secret
# The only hosts a local model is reached on: this node's loopback, or the name a
# container resolves to its Docker host. Never a network address.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})
_PLACEHOLDER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


def validate_local_base_url(url: object) -> str:
    """The URL the local model answers on: http(s), a loopback or Docker-host
    name, no credentials, no path, query or fragment. This is also the exact URL a
    worker may redirect the Anthropic API to, so it is normalised here."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("a local account needs a base_url")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base_url must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("base_url must not carry credentials, a query or a fragment")
    if parts.path.strip("/"):
        raise ValueError("base_url must not carry a path")
    if parts.hostname.lower() not in LOCAL_HOSTS:
        raise ValueError(
            "base_url host must be one of "
            f"{', '.join(sorted(LOCAL_HOSTS))}: the local model is never reached "
            "over the network"
        )
    return f"{parts.scheme}://{parts.netloc}"


def local_credential_problem(credential: dict[str, Any]) -> str | None:
    """Why this document cannot be a local account's credential, or None. A local
    account holds no secret: a real-looking token is refused, not stored."""
    unknown = sorted(set(credential) - LOCAL_CREDENTIAL_KEYS)
    if unknown:
        return (
            "a local account credential holds only "
            f"{', '.join(sorted(LOCAL_CREDENTIAL_KEYS))}; remove {', '.join(unknown)}"
        )
    try:
        validate_local_base_url(credential.get("base_url"))
    except ValueError as exc:
        return str(exc)
    token = credential.get("auth_token", LOCAL_AUTH_TOKEN)
    if (
        not isinstance(token, str)
        or not _PLACEHOLDER.fullmatch(token)
        or token.startswith("sk-")
    ):
        return (
            "auth_token must be a short placeholder (the local server ignores it), "
            "never a credential"
        )
    model = credential.get("model")
    if model is not None and (
        not isinstance(model, str) or not _MODEL_NAME.fullmatch(model)
    ):
        return "model must be a plain model name"
    ctx = credential.get("context_tokens")
    low, high = LOCAL_CONTEXT_TOKENS_RANGE
    if ctx is not None and (
        not isinstance(ctx, int) or isinstance(ctx, bool) or not low <= ctx <= high
    ):
        return f"context_tokens must be an integer from {low} to {high}"
    return None


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
    # `custom` accounts only: base URL, label, per-role model and routing options.
    config: CustomConfig | None = None

    _limits = field_validator("token_limits")(_check_token_limits)

    @model_validator(mode="after")
    def one_secret_source(self) -> "AccountCreate":
        if (self.credential is None) == (self.secret_ref is None):
            raise ValueError("give exactly one of credential or secret_ref")
        if (self.provider == CUSTOM_PROVIDER) != (self.config is not None):
            raise ValueError("config is required for, and only for, provider custom")
        return self

    @model_validator(mode="after")
    def local_account_carries_no_secret(self) -> "AccountCreate":
        """A local account's document is its endpoint, not a credential, and it
        defaults to one lease: the node has one GPU and the server one slot."""
        if self.provider != LOCAL_PROVIDER:
            return self
        if self.credential is None:
            raise ValueError(
                "a local account carries its base_url as the credential, "
                "not a secret_ref"
            )
        problem = local_credential_problem(self.credential)
        if problem is not None:
            raise ValueError(problem)
        if self.max_leases is None:
            self.max_leases = 1
        return self


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    priority: int | None = None
    max_leases: int | None = Field(default=None, ge=1)
    labels: dict[str, str] | None = None
    enabled: bool | None = None
    billing_day: int | None = Field(default=None, ge=1, le=31)
    token_limits: dict[str, int] | None = None  # null clears
    config: CustomConfig | None = None  # custom accounts: replaces the whole config

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
    # `custom` accounts: where and how to call. Never contains the key.
    config: CustomConfig | None = None
    created_at: datetime
    updated_at: datetime


class LeaseRequest(BaseModel):
    """Lease the best usable account globally (priority, then least leased).

    The retired `providers` list is accepted and ignored: no caller-side order or
    allowlist restricts selection."""

    # Providers the runtime can technically apply (it knows how to inject them).
    # Required for credential delivery; capability, not preference, so it never orders.
    runtime_providers: list[str] = Field(min_length=1)
    # `engine`: the account the worker's harness runs on; `executor`: the one a
    # CLI it delegates to uses. One active lease per role.
    role: LeaseRole = "engine"
    # Executor role only: when the best account is one this agent already holds as
    # its engine, return that lease unchanged and take no extra slot (a delegated
    # CLI of the same provider runs on the engine's own credential). Otherwise the
    # executor lease is an ordinary separate one.
    reuse_held: bool = False
    # Credential delivery is bound to the active claim that needs it.
    task_id: str
    attempt: int = Field(ge=1)


class AccountLease(BaseModel):
    """What a leasing agent receives, including the credential to use."""

    lease_id: str
    account_id: str
    name: str
    provider: str
    kind: AccountKind
    role: LeaseRole = "engine"
    credential: dict[str, Any]
    # `custom` accounts: base URL, model and `provider` block for this role.
    settings: LeaseSettings | None = None


class ExhaustedReport(BaseModel):
    """Mark a held account exhausted for the caller's live task claim.

    `runtime_providers` declares which fallback account metadata the runtime can
    use. The response never carries the next credential.
    """

    until: datetime | None = None  # the provider's reset time, if known
    reason: str = Field(default="", max_length=2000)
    runtime_providers: list[str] = Field(min_length=1)
    # Which of the agent's leases hit the limit (it may hold one account twice).
    role: LeaseRole = "engine"
    # The task the agent was working on: blocked as `awaiting_quota` if nothing is left.
    task_id: str
    attempt: int = Field(ge=1)
    # Provenance of the report (account selection 3.6). All optional: omitted, the
    # server classifies `reason` itself.
    source: Literal["structured", "notice", "log"] | None = None
    reason_code: Literal["quota_exhausted", "auth_expired"] | None = None
    reset_status: Literal["stated", "ambiguous", "absent"] | None = None
    reset_text: str | None = Field(default=None, max_length=120)  # provider's words


class AwaitingQuotaRequest(BaseModel):
    """An agent with no usable account asks for the work it cannot claim to wait."""

    capabilities: list[str] = []
    runtime_providers: list[str] = Field(min_length=1)


class AwaitingQuotaResult(BaseModel):
    task_ids: list[str]
    refill_at: datetime | None = None  # earliest exhausted_until


class FallbackResult(BaseModel):
    lease: AccountLease | None = None
    next_account_id: str | None = None
    next_account_name: str | None = None
    next_provider: str | None = None
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
    """E.g. refreshed OAuth tokens for a subscription account.

    An agent must name the live task claim it rotates for; an admin need not."""

    credential: dict[str, Any]
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
