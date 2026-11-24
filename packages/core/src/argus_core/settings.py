"""Typed global settings with per-project overrides (docs/specs/settings.spec.md)."""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SettingScope = Literal["global", "project"]
SettingSource = Literal["project", "global", "default", "env", "file"]
ReviewMode = Literal["off", "different_model", "different_provider"]
VerifyFindings = Literal["off", "observe", "enforce"]


class IndependentReviewSetting(BaseModel):
    """Key `review.independent`: require a different model to review delivered code."""

    model_config = ConfigDict(extra="forbid")

    mode: ReviewMode = "off"
    max_rounds: int = Field(default=3, ge=1, le=20)
    # Reviewer children that may fail without a verdict before an operator decides.
    max_reviewer_failures: int = Field(default=2, ge=0, le=20)
    # Repair attempts for an approved PR that breaks (PR steward spec 3.2); 0 is off.
    max_repairs: int = Field(default=0, ge=0, le=3)
    # A read-only verifier tries to refute each request_changes finding (review finding
    # verification spec 3.4): `observe` only records, `enforce` holds the verdict.
    verify_findings: VerifyFindings = "off"


ReviewPolicyMode = Literal["off", "observe", "enforce"]
ReviewTier = Literal["skip", "standard", "human"]
# Strictest last: the head takes the strictest tier of its paths.
TIER_ORDER: tuple[ReviewTier, ...] = ("skip", "standard", "human")
MAX_POLICY_RULES = 50
MAX_RULE_PATHS = 50
_RULE_NAME = re.compile(r"^[a-z0-9-]{1,40}$")
_GLOB_FORBIDDEN = re.compile(r"[\\\[\]{}!\x00-\x1f]")


def _check_glob(glob: str) -> str:
    if not glob or len(glob) > 200:
        raise ValueError("a glob is 1 to 200 characters")
    if glob.startswith("/") or _GLOB_FORBIDDEN.search(glob):
        raise ValueError(
            "a glob is a relative POSIX path pattern: no negation, character class,"
            " braces or backslash"
        )
    for segment in glob.split("/"):
        if not segment:
            raise ValueError("a glob has no empty path segment")
        if "**" in segment and segment != "**":
            raise ValueError("`**` must be a whole path segment")
    return glob


class ReviewPolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tier: ReviewTier
    paths: list[str] = Field(min_length=1, max_length=MAX_RULE_PATHS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _RULE_NAME.fullmatch(value):
            raise ValueError("a rule name is 1 to 40 characters of [a-z0-9-]")
        return value

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        return [_check_glob(g) for g in value]


class ReviewPolicySetting(BaseModel):
    """Key `review.policy`: path-based review tiers (review policy spec 3.1)."""

    model_config = ConfigDict(extra="forbid")

    mode: ReviewPolicyMode = "off"
    # The tier of a path no rule matches; never `skip`.
    default: Literal["standard", "human"] = "standard"
    rules: list[ReviewPolicyRule] = Field(
        default_factory=list, max_length=MAX_POLICY_RULES
    )

    @field_validator("rules")
    @classmethod
    def _unique(cls, value: list[ReviewPolicyRule]) -> list[ReviewPolicyRule]:
        names = [r.name for r in value]
        if len(set(names)) != len(names):
            raise ValueError("rule names must be unique")
        return value


class SettingDefinition(BaseModel):
    key: str
    # `schema` is the JSON Schema for the value; aliased so it serialises as `schema`.
    schema_: dict[str, Any] = Field(
        validation_alias="schema", serialization_alias="schema"
    )
    default: Any
    scopes: list[SettingScope]
    description: str

    model_config = ConfigDict(populate_by_name=True)


class SettingValue(BaseModel):
    key: str
    value: Any
    source: SettingSource
    updated_at: datetime | None = None
    updated_by: str | None = None
    # `models` only: content revision of the effective (merged) policy, and a notice
    # when ARGUS_MODEL_POLICY locks edits out.
    revision: str | None = None
    notice: str | None = None
    scoped_value: Any = None
    difficulty_sources: dict[str, str] | None = None
    difficulty_revisions: dict[str, str] | None = None
    # `execution.chains`: readiness warnings for referenced accounts whose state
    # allows saving but not admission. Never availability or credential data.
    warnings: list[str] = []


class SettingsResponse(BaseModel):
    scope: str  # "global" or a project slug
    settings: list[SettingValue]


class SettingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
