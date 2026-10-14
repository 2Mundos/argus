"""Task kinds: optional schemas and defaults per kind (worker lifecycle 3.11)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from argus_core.tasks import Review

KIND_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class TaskKindUpsert(BaseModel):
    description: str = ""
    input_schema: dict[str, Any] | None = None
    result_schema: dict[str, Any] | None = None
    default_capabilities: list[str] | None = None
    default_lease_ttl_s: int | None = Field(default=None, ge=10, le=3600)
    default_max_attempts: int | None = Field(default=None, ge=1, le=20)
    default_review: Review | None = None


class TaskKind(TaskKindUpsert):
    name: str
    created_at: datetime
    updated_at: datetime
