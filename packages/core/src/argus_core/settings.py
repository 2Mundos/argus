"""Typed global settings with per-project overrides (docs/specs/settings.spec.md)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SettingScope = Literal["global", "project"]
SettingSource = Literal["project", "global", "default", "env", "file"]
ReviewMode = Literal["off", "different_model", "different_provider"]


class IndependentReviewSetting(BaseModel):
    """Key `review.independent`: require a different model to review delivered code."""

    model_config = ConfigDict(extra="forbid")

    mode: ReviewMode = "off"
    max_rounds: int = Field(default=3, ge=1, le=20)


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


class SettingsResponse(BaseModel):
    scope: str  # "global" or a project slug
    settings: list[SettingValue]


class SettingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
