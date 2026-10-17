"""Provider account pool (worker lifecycle 3.9)."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

LeaseRole = Literal["engine", "executor", "repository"]


class AccountKind(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


class AccountState(StrEnum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"  # until exhausted_until, then available again
    DISABLED = "disabled"


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

    @model_validator(mode="after")
    def one_secret_source(self) -> "AccountCreate":
        if (self.credential is None) == (self.secret_ref is None):
            raise ValueError("give exactly one of credential or secret_ref")
        return self


class AccountUpdate(BaseModel):
    priority: int | None = None
    max_leases: int | None = Field(default=None, ge=1)
    labels: dict[str, str] | None = None
    enabled: bool | None = None


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float


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
    created_at: datetime
    updated_at: datetime


class LeaseRequest(BaseModel):
    """Providers the agent's runtime can use, most preferred first."""

    providers: list[str] = Field(min_length=1)
    # `engine`: the account the worker's harness runs on; `executor`: the one a
    # CLI it delegates to uses. One active lease per role.
    role: LeaseRole = "engine"


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
    # Which of the agent's leases hit the limit (it may hold one account twice).
    role: LeaseRole = "engine"
    # The task the agent was working on: blocked as `no_account` if nothing is left.
    task_id: str | None = None
    attempt: int | None = None


class FallbackResult(BaseModel):
    lease: AccountLease | None
    earliest_reset: datetime | None = None
    task_blocked: bool = False


class UsageReport(BaseModel):
    task_id: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class CredentialUpdate(BaseModel):
    """E.g. refreshed OAuth tokens for a subscription account."""

    credential: dict[str, Any]
