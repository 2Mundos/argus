"""Task drafts: editable definitions that are not tasks until published.

See docs/specs/task-drafts.spec.md.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus_core.tasks import TaskCreate, TaskState

MAX_OPEN_DRAFTS = 200
MAX_PUBLISH_BATCH = 50
MAX_DRAFT_DEPENDENCIES = 100  # depends_on + depends_on_drafts (task dependencies 3.1)


class TaskDraftBody(TaskCreate):
    """`TaskCreate` with the same field rules, lenient only about completeness:
    the title may be empty until publish (3.1). Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    # Other drafts this one depends on; at publish they become the new task ids.
    depends_on_drafts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dependency_count(self) -> "TaskDraftBody":
        if len(self.depends_on) + len(self.depends_on_drafts) > MAX_DRAFT_DEPENDENCIES:
            raise ValueError(f"at most {MAX_DRAFT_DEPENDENCIES} dependencies")
        return self


class TaskDraft(BaseModel):
    id: str
    body: TaskDraftBody
    version: str  # updated_at, the edit and publish precondition
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    published_at: datetime | None = None
    published_task_id: str | None = None


class TaskDraftPage(BaseModel):
    items: list[TaskDraft]  # open drafts, oldest first; whole (<= MAX_OPEN_DRAFTS)
    total: int


class TaskDraftEdit(BaseModel):
    """A partial patch of the body: a key left out is kept, `null` resets it."""

    expected_version: str = Field(min_length=1)
    changes: dict[str, Any] = Field(min_length=1)


class PublishItem(BaseModel):
    draft_id: str = Field(min_length=1)
    expected_version: str = Field(min_length=1)


class PublishRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    mode: Literal["atomic", "partial"] = "atomic"
    drafts: list[PublishItem] = Field(min_length=1, max_length=MAX_PUBLISH_BATCH)

    @model_validator(mode="after")
    def _distinct(self) -> "PublishRequest":
        ids = [d.draft_id for d in self.drafts]
        if len(set(ids)) != len(ids):
            raise ValueError("each draft may be named once")
        return self


class PublishOutcome(BaseModel):
    draft_id: str
    outcome: Literal["published", "rejected", "skipped"]
    task_id: str | None = None
    state: TaskState | None = None
    code: str | None = None  # why it was rejected
    errors: list[str] = []


class PublishResult(BaseModel):
    idempotency_key: str
    mode: Literal["atomic", "partial"]
    replayed: bool = False  # the stored answer to an earlier identical request
    items: list[PublishOutcome]
