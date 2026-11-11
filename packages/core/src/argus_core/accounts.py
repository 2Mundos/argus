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
# The node's own model, served on loopback (local harness spec): harness and/or
# OpenCode executor, holding no real credential and no quota of its own.
LOCAL_PROVIDER = "local"
# Providers that never serve a harness (the `engine` role). Empty today: `local`
# is both harness and OpenCode executor (local harness spec).
ENGINE_ONLY: frozenset[str] = frozenset()
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


# A local account's whole credential: where workers reach the model (`base_url`),
# optionally where the Argus API process reaches it (`api_base_url`), the
# placeholder the harness sends instead of a token, the model name it configures
# and, optionally, the model's context window in tokens (`context_tokens`; when
# absent the lease carries the window the account's probe measured). The harness
# compacts well before that window (local harness spec, "Harness orchestrates only").
LOCAL_CREDENTIAL_KEYS = frozenset(
    {"base_url", "api_base_url", "auth_token", "model", "context_tokens"}
)
# What a lease credential carries: the keys a deployed NanoClaw accepts. The API
# endpoint is resolved by the server per runtime, never shipped to a worker.
LOCAL_WORKER_KEYS = frozenset({"base_url", "auth_token", "model", "context_tokens"})
DOCKER_HOST = "host.docker.internal"
LOCAL_CONTEXT_TOKENS_RANGE = (4096, 4_000_000)
LOCAL_AUTH_TOKEN = "argus-local-harness"  # noqa: S105 — public placeholder, not a secret
# The only hosts a local model is reached on: this node's loopback, or the name a
# container resolves to its Docker host. Never a network address.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})
_PLACEHOLDER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


def validate_local_base_url(url: object, field: str = "base_url") -> str:
    """The URL the local model answers on: http(s), a loopback or Docker-host
    name, no credentials, no path, query or fragment. This is also the exact URL a
    worker may redirect the Anthropic API to, so it is normalised here. `field`
    names the credential key in errors (`base_url` or `api_base_url`)."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"a local account needs a {field}")
    parts = urlsplit(url.strip())
    try:
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        raise ValueError(f"{field} must carry a valid port") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{field} must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{field} must not carry credentials, a query or a fragment")
    if parts.path.strip("/"):
        raise ValueError(f"{field} must not carry a path")
    if parts.hostname.lower() not in LOCAL_HOSTS:
        raise ValueError(
            f"{field} host must be one of "
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
        if credential.get("api_base_url") is not None:
            validate_local_base_url(credential["api_base_url"], "api_base_url")
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


LocalEndpointSource = Literal["api_base_url", "derived", "base_url"]


def local_api_endpoint(
    credential: dict[str, Any], *, in_container: bool
) -> tuple[str, LocalEndpointSource]:
    """Where the Argus API process (built-in orchestrator, health probe, admin
    test) reaches a valid local credential's model, and why (local harness spec,
    "One endpoint per runtime"): the stated `api_base_url`; else, outside a
    container, a `host.docker.internal` worker URL's port on 127.0.0.1 (the host
    side of the bridge forwarder: that name resolves only inside containers);
    else the worker `base_url` itself."""
    if credential.get("api_base_url") is not None:
        return (
            validate_local_base_url(credential["api_base_url"], "api_base_url"),
            "api_base_url",
        )
    base = validate_local_base_url(credential.get("base_url"))
    parts = urlsplit(base)
    if not in_container and (parts.hostname or "").lower() == DOCKER_HOST:
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://127.0.0.1{port}", "derived"
    return base, "base_url"


def local_worker_credential(credential: dict[str, Any]) -> dict[str, Any]:
    """The credential as a lease delivers it: only the keys a worker accepts."""
    return {k: v for k, v in credential.items() if k in LOCAL_WORKER_KEYS}


def local_lease_settings(credential: dict[str, Any]) -> LeaseSettings | None:
    """OpenCode (and similar) settings from a local credential: base URL and model,
    label `local`, no provider block. None when the document is not a valid local
    credential (local harness spec, local as executor)."""
    if local_credential_problem(credential) is not None:
        return None
    model = credential.get("model")
    return LeaseSettings(
        base_url=validate_local_base_url(credential["base_url"]),
        label="local",
        model=model if isinstance(model, str) else None,
        provider=None,
    )


def is_model_name(value: object) -> bool:
    """A plain model name, as a local account's `model` must be."""
    return isinstance(value, str) and _MODEL_NAME.fullmatch(value) is not None


class LocalCandidate(BaseModel):
    """A model server that answered `/v1/models` on this node
    (builtin-orchestrator spec 3.11)."""

    base_url: str  # what the account stores: the worker endpoint
    # What the Argus API process uses for that account (local harness spec).
    api_endpoint: str
    server_hint: Literal["ollama", "lmstudio", "llamacpp"]
    models: list[str]
    # Workers can reach `base_url` (a host.docker.internal address).
    preferred: bool
    note: str | None = None


class LocalDetection(BaseModel):
    candidates: list[LocalCandidate]
    hint: str | None = None


# One classification of a failure to reach or use a model endpoint (local harness
# spec, "Failures"), from Argus's own transport and the HTTP status, never text.
LocalFailure = Literal[
    "dns_failed",
    "connection_refused",
    "timeout",
    "tls_failed",
    "connect_failed",
    "redirect_refused",
    "http_error",
    "invalid_response",
    "model_not_served",
    "no_models",
    "no_tool_call",
    "empty_answer",
]
LocalCheckStatus = Literal["passed", "failed", "skipped", "not_run"]


class LocalCheck(BaseModel):
    """One check of a local account, from the Argus API process. `detail` is
    Argus's own sentence, never the server's text."""

    status: LocalCheckStatus = "not_run"
    failure: LocalFailure | None = None
    http_status: int | None = None
    detail: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    at: datetime | None = None


OrchestratorModelSource = Literal["orchestrator.model", "account", "served"]


class LocalAccountView(BaseModel):
    """A local account's endpoints, health and last test (local harness spec,
    "Account view"). Never the placeholder token."""

    base_url: str  # the worker endpoint
    api_base_url: str | None  # stored override, or null
    api_endpoint: str  # what the Argus API process uses
    api_endpoint_source: LocalEndpointSource
    model: str | None
    context_tokens: int | None
    orchestrator_model: str | None  # what the built-in orchestrator would run
    orchestrator_model_source: OrchestratorModelSource | None
    served_models: list[str] | None  # null: not probed yet
    discovery: LocalCheck
    inference: LocalCheck
    orchestrator: LocalCheck
    warnings: list[str] = []


class LocalTestRequest(BaseModel):
    """An admin's explicit test. Discovery always runs; the two completions run
    only when asked (no automatic inference)."""

    inference: bool = False
    tool_calling: bool = False


class LocalTestResult(BaseModel):
    account_id: str
    account_name: str
    runtime: Literal["api"] = "api"
    endpoint: str
    endpoint_source: LocalEndpointSource
    worker_endpoint: str
    # Why the worker endpoint was not tested from here, when it differs.
    worker_note: str | None = None
    served_models: list[str]
    discovery: LocalCheck
    inference: LocalCheck
    orchestrator: LocalCheck
    warnings: list[str] = []


class LocalAccountUpdate(BaseModel):
    """The local editor: a field left out is kept; `null` clears an optional one
    (`api_base_url`, `model`, `context_tokens`). `base_url` cannot be cleared."""

    base_url: str | None = None
    api_base_url: str | None = None
    model: str | None = None
    context_tokens: int | None = None

    @model_validator(mode="after")
    def keeps_a_worker_endpoint(self) -> "LocalAccountUpdate":
        if "base_url" in self.model_fields_set and self.base_url is None:
            raise ValueError("base_url cannot be cleared: workers need it")
        if not self.model_fields_set:
            raise ValueError("give at least one field to change")
        return self


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


class CredentialState(BaseModel):
    """What Argus stores about a credential, never a verdict that it works: `present`
    is not "healthy" (account-usage.spec.md 3.5). `expires_at` is the stored Claude
    `expiresAt`; the holder may have refreshed the token since."""

    state: Literal["present", "missing", "expired"]
    expires_at: datetime | None = None


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
    # Derived on read from the secret store; null where no store was consulted.
    credential: CredentialState | None = None
    # `custom` accounts: where and how to call. Never contains the key.
    config: CustomConfig | None = None
    # `local` accounts: endpoints per runtime, health and last test; set on read.
    local: LocalAccountView | None = None
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
    # Narrows eligibility to accounts of these credential kinds and never orders
    # them, like `runtime_providers` (the built-in orchestrator sends `api_key`,
    # builtin-orchestrator spec 3.4). Absent: every kind.
    credential_kinds: list[AccountKind] | None = Field(default=None, min_length=1)

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
    # Internal orchestrator only: the harness model `orchestrator.model` names, set
    # when this lease is on that model's provider (internal orchestrator spec 3.1).
    model: str | None = None


class NativeSessionReport(BaseModel):
    """The engine's own session the ending attempt left behind (quota resume spec 3.2).
    Optional on a quota report and on a confirmed `session_lost` report; the server
    adds the node and ignores it for any other ending."""

    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    engine: str = Field(min_length=1, max_length=32)


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
    # Both absent only for the internal orchestrator's engine (internal orchestrator 3.1).
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    # Provenance of the report (account selection 3.6). All optional: omitted, the
    # server classifies `reason` itself.
    source: Literal["structured", "notice", "log"] | None = None
    reason_code: Literal["quota_exhausted", "auth_expired"] | None = None
    reset_status: Literal["stated", "ambiguous", "absent"] | None = None
    reset_text: str | None = Field(default=None, max_length=120)  # provider's words
    native_session: NativeSessionReport | None = None
    # The same narrowing as on the lease, so the named next account is one the
    # reporting runtime can lease (builtin-orchestrator spec 3.4).
    credential_kinds: list[AccountKind] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _reset_status_matches_until(self) -> "ExhaustedReport":
        """`stated` is a zoned time; `absent` and `ambiguous` carry none."""
        if (self.task_id is None) != (self.attempt is None):
            raise ValueError("give both task_id and attempt, or neither")
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

    # Both absent only for the internal orchestrator's engine (internal orchestrator 3.1).
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    role: LeaseRole = "engine"
    source: Literal["structured", "notice", "log"] | None = None

    @model_validator(mode="after")
    def claim_given_whole(self) -> "AuthRejectedReport":
        if (self.task_id is None) != (self.attempt is None):
            raise ValueError("give both task_id and attempt, or neither")
        return self


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


class ProvisionalUsageReport(BaseModel):
    """A running executor's cumulative usage so far (task cost spec 3.8). Replaced
    by the run's final usage row; a revision at or below the stored one adds nothing."""

    task_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    run_key: str = Field(pattern=r"^r1:[A-Za-z0-9._:-]{1,196}$")
    revision: int = Field(ge=1, le=2**53 - 1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    model: str | None = Field(default=None, max_length=100)
    engine: str | None = Field(default=None, max_length=50)


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
