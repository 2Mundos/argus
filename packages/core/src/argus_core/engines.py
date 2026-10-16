"""Engines, executors and the local model (engines spec)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argus_core.criteria_safe import assert_schema_definition, criteria_is_effective

_DEFAULTS = ConfigDict(json_schema_serialization_defaults_required=True)


class EngineRole(StrEnum):
    ORCHESTRATOR = "orchestrator"  # runs the worker's own agent loop
    CODE_EXECUTOR = "code_executor"  # preferred target for code delegation
    EXECUTOR = "executor"  # delegated non-interactive CLI
    LOCAL = "local"  # local model reached through Argus


class Fact(BaseModel):
    """One readiness fact: true, false, or unknown (None) with its evidence."""

    model_config = _DEFAULTS

    ok: bool | None = None
    detail: str = "not checked"
    at: datetime | None = None


class Readiness(BaseModel):
    """Installed != authenticated != validated; usable only if all three hold."""

    model_config = _DEFAULTS

    installed: Fact = Fact()
    authenticated: Fact = Fact()
    validated: Fact = Fact()

    @property
    def usable(self) -> bool:
        return all(
            f.ok is True for f in (self.installed, self.authenticated, self.validated)
        )


class Axes(BaseModel):
    model_config = _DEFAULTS

    model_provider: str  # whose model answers
    model: str | None  # None: chosen by the CLI/account, not by Argus
    harness: str  # what runs the loop: nanoclaw provider, a CLI, or "argus-router"
    executor: str | None  # CLI delegated to, None for the local model
    account_provider: str | None  # Argus accounts `provider` string, or None


class AccountsSummary(BaseModel):
    """Registry accounts behind an engine, by state. Counts only."""

    model_config = _DEFAULTS

    available: int = 0
    exhausted: int = 0
    disabled: int = 0


class EngineEntry(BaseModel):
    model_config = _DEFAULTS

    id: str
    label: str
    role: EngineRole
    axes: Axes
    auth_methods: list[str]  # real ways to authenticate, never secrets
    version: str | None = None
    readiness: Readiness = Readiness()
    # None: the engine has no account provider (or no registry was consulted).
    accounts: AccountsSummary | None = None
    usable: bool = False
    notes: str = ""


class LocalModelInfo(BaseModel):
    """Measured from the server, never configured."""

    model_config = _DEFAULTS

    reachable: bool
    model: str | None = None
    n_ctx: int | None = None
    slots: int | None = None
    error: str | None = None


class EngineCatalog(BaseModel):
    model_config = _DEFAULTS

    engines: list[EngineEntry]
    local: LocalModelInfo | None  # None: no local model configured


class RouteRequest(BaseModel):
    model_config = _DEFAULTS

    summary: str = Field(max_length=2000)
    simple: bool = False  # caller asserts: small, single-step
    low_risk: bool = False
    criteria_deterministic: bool = False  # acceptance can be checked by code
    multi_file: bool = False
    touches_sensitive: bool = False  # auth, security, secrets, money
    is_code: bool = False
    # Everything the model would see, in characters: instructions, tool
    # schemas, history, inputs. Unknown (None) never routes to the local model.
    input_chars: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(default=512, ge=1)


class RouteDecision(BaseModel):
    model_config = _DEFAULTS

    target: str  # engine id: "local", "codex", "claude", ...
    reason: str
    local_considered: bool
    local_rejections: list[str]
    estimated_input_tokens: int | None
    context_tokens: int | None
    model: str | None  # effective model when known
    blocked: str | None = None  # the wanted target is unusable: why
    fallback_from: str | None = None  # the engine wanted before moving to `target`
    # Preferred code executor the engine delegates to (code tasks), when usable.
    executor: str | None = None
    escalate_to: str | None = None


class Criteria(BaseModel):
    """Deterministic acceptance checks for a local answer."""

    model_config = _DEFAULTS

    json_schema: dict | None = None
    must_contain: list[str] = []
    equals: str | None = Field(default=None, max_length=10_000)
    max_chars: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_must_match(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("must_match") is not None:
            raise ValueError(
                "must_match is not supported; use equals for a literal match"
            )
        return data

    @field_validator("json_schema")
    @classmethod
    def safe_json_schema(cls, value: dict | None) -> dict | None:
        if value is not None:
            assert_schema_definition(value)
        return value


class LocalRunRequest(BaseModel):
    model_config = _DEFAULTS

    prompt: str = Field(min_length=1, max_length=200_000)
    system: str | None = Field(default=None, max_length=50_000)
    criteria: Criteria = Criteria()
    max_output_tokens: int = Field(default=512, ge=1, le=8192)
    max_attempts: int = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def require_effective_criteria(self) -> "LocalRunRequest":
        if not criteria_is_effective(self.criteria):
            raise ValueError(
                "criteria must include at least one acceptance check "
                "(must_contain, equals, max_chars, or json_schema)"
            )
        return self


class LocalAttempt(BaseModel):
    model_config = _DEFAULTS

    text: str | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    failures: list[str]  # criteria that did not hold, or why the call failed


class LocalRunResult(BaseModel):
    model_config = _DEFAULTS

    verdict: str  # "accepted" | "escalate"
    model: str
    attempts: list[LocalAttempt]
    text: str | None  # the accepted answer, else None
    escalate_to: str | None
    cost: None = None  # unknown, deliberately not 0
