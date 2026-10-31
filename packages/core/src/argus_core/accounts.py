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
# providers of repository access, and the one provider that only a delegated CLI
# (never a NanoClaw harness) can run on.
GITHUB_HOST = "github.com"
REPOSITORY_PROVIDERS = frozenset({"gitea", "github"})
EXECUTOR_ONLY = frozenset({"cursor", "copilot"})
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
        return provider in REPOSITORY_PROVIDERS
    if provider in REPOSITORY_PROVIDERS:
        return False
    if role == "engine" and provider in EXECUTOR_ONLY:
        return False
    return not (role != "engine" and provider in ENGINE_ONLY)


def repository_provider(host: str) -> str:
    """The repository provider of a forge host: github.com is GitHub, every other
    host Gitea (GitHub repositories spec 3.1)."""
    return "github" if host.lower() == GITHUB_HOST else "gitea"


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
    # Runs that carried figures. 0 means the tokens above were never measured
    # (read them as "not measured", not as zero tokens).
    measured_runs: int = 0
    # Unmeasured runs by reason code (`unknown` when the reporter gave none).
    unmeasured_reasons: dict[str, int] = {}
    # All runs by the lease role that ran them: engine, executor, unknown.
    runs_by_role: dict[str, int] = {}


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


ProbeStatus = Literal["updated", "skipped", "rate_limited", "failed"]
QuotaCollector = Literal[
    "codex_rate_limits",
    "claude_oauth_usage",
    "cursor_dashboard",
    "openrouter_key",
    "worker_report",
]
# Why there is no (fresh) figure. Each is a different fact: a source that does not
# exist, one that needs something Argus does not hold, one that failed this time.
QuotaStatusReason = Literal[
    "no_usage_records",
    "provider_does_not_expose",  # kept valid; new code prefers the codes below
    "window_not_configured",
    "unsupported_provider",
    "api_key_account",  # an API key: the provider offers no window to read with it
    "not_yet_probed",  # nothing has tried to read it yet
    "probe_failed",
    "probe_skipped",
    "probe_rate_limited",
    "probe_disabled",  # the reader is not installed/configured on this node
    "credential_missing",
    "credential_invalid",
    "credential_expired",  # Argus never refreshes a login to read quota
    "credential_rejected",  # the provider answered 401
    "oauth_scope_missing",  # the token lacks the scope the usage source needs
    "admin_key_required",  # an API key reads usage only through the team Admin API
    "no_provider_source",  # no read-only quota source exists for this account
    "local_no_quota",  # the node's own model has no provider quota
    "provider_rate_limited",  # the provider answered 429
    "fetch_failed",  # network error or a 5xx
    "bad_response",  # the provider answered something we do not understand
    "no_windows",  # understood, but it carried no window
    "no_usage_window_for_login",  # the login answered; no numeric usage or grant
    "window_locked",  # the provider locked the window: no usage figure to show
]


class QuotaProbe(BaseModel):
    """The latest quota probe of an account (one per account), whatever came of it."""

    status: ProbeStatus
    code: QuotaStatusReason | None = None  # machine-readable cause when not updated
    reason: str | None = None  # short, never a credential value
    at: datetime


class AccountCredit(BaseModel):
    """What a provider says is left to spend on an account or key (money, not a
    window). `limit`/`remaining` are null when the key has no cap; the figure is the
    key's, not the provider account's balance. `unit` is never tokens."""

    unit: Literal["usd"]
    limit: float | None
    remaining: float | None
    used: float | None
    limit_reset: str | None = None  # "daily" | "weekly" | "monthly" | null: never
    resets_at: datetime | None = None  # a stated reset instant (billing cycle)
    free_tier: bool | None = None
    observed_at: datetime
    source: Literal["polled"]
    collector: QuotaCollector | None = None


class QuotaStatus(BaseModel):
    """Whether Argus knows this account's quota, and if not, why (never a 0/100).

    `known`: provider snapshots all fresh, or local estimates against
    `token_limits`. `stale`: snapshots exist (listed in `quota`) but one was
    observed over an hour ago or its window has already reset. `unknown`: no
    figure, with the reason."""

    state: Literal["known", "stale", "unknown"]
    reason: QuotaStatusReason | None = None
    windows: list[QuotaStatusWindow] = []
    observed_at: datetime | None = None  # newest provider snapshot
    collector: QuotaCollector | None = None  # who produced that snapshot
    last_probe: QuotaProbe | None = None
    credit: AccountCredit | None = None


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
    credit: AccountCredit | None = None
    token_limits: dict[str, int] | None = None
    billing_day: int | None = None
    # Past evidence only (account-usage.spec.md 3.5); never a healthy verdict.
    last_accepted_at: datetime | None = None
    last_rejected_at: datetime | None = None
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
    # Credential delivery is bound to the active claim that needs it. Only the
    # internal orchestrator, which runs conversations instead of tasks, leases its
    # engine without one (internal orchestrator spec 3.1).
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def claim_given_whole(self) -> "LeaseRequest":
        if (self.task_id is None) != (self.attempt is None):
            raise ValueError("give both task_id and attempt, or neither")
        return self


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

    @model_validator(mode="after")
    def _reset_status_matches_until(self) -> "ExhaustedReport":
        """`stated` is a zoned time; `absent` and `ambiguous` carry none."""
        if self.reset_status == "stated":
            if self.until is None or self.until.tzinfo is None:
                raise ValueError("reset_status 'stated' needs a time with a zone")
        elif self.reset_status is not None and self.until is not None:
            raise ValueError(f"reset_status {self.reset_status!r} cannot carry until")
        return self


class AuthRejectedReport(BaseModel):
    """Record that the provider rejected the stored credential (account usage 3.5).

    Does not exhaust the account or change its state; only writes `account.auth_rejected`.
    """

    task_id: str
    attempt: int = Field(ge=1)
    role: LeaseRole = "engine"
    source: Literal["structured", "notice", "log"] | None = None


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


# The API's own attempt placeholder key (account usage 3.10); never a client's.
PLACEHOLDER_PREFIX = "a1:"


class UsageReport(BaseModel):
    """One engine run on one held account (account usage 3.2)."""

    task_id: str | None = None
    # The task attempt of the run (3.10); without it, the agent's latest claim.
    attempt: int | None = Field(default=None, ge=1)
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
    # Why nothing was measured (not_reported only), e.g. `no_usage_in_output`.
    reason: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,60}$")

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
        if self.reason is not None and self.measurement != "not_reported":
            raise ValueError("reason is only allowed with measurement=not_reported")
        if self.measurement == "measured" and all(f is None for f in figures):
            raise ValueError(
                "a measured run needs tokens or cost; use measurement=not_reported"
            )
        if self.run_key is not None and self.run_key.startswith(PLACEHOLDER_PREFIX):
            raise ValueError(f"run_key prefix {PLACEHOLDER_PREFIX!r} is reserved")
        return self


class UsageBucket(BaseModel):
    start: datetime  # in the requested timezone
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    runs: int
    unmeasured_runs: int = 0
    measured_runs: int = 0


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
    status: ProbeStatus
    code: QuotaStatusReason | None = None
    reason: str | None = None
    quota: list[QuotaWindow]
    credit: AccountCredit | None = None
    quota_status: QuotaStatus | None = None  # what the account now reads as


class CredentialUpdate(BaseModel):
    """E.g. refreshed OAuth tokens for a subscription account.

    An agent must name the live task claim it rotates for; an admin need not."""

    credential: dict[str, Any]
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
