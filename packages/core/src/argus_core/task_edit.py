"""Which task fields can be edited, in which state, and the patch model.

See docs/specs/task-edit.spec.md. REST, MCP and the dashboard all read the
policy from here.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from argus_core.difficulty import Difficulty
from argus_core.tasks import AccountRef, ExecutorModel, HarnessModel, TaskState

EDITABLE = (
    "title",
    "spec",
    "acceptance",
    "priority",
    "required_capabilities",
    "knowledge",
    "depends_on",
    "difficulty",
    "account_providers",
    "harness_model",
    "executor_model",
    "harness_account",
)
# `null` clears these to their empty value; title, priority and difficulty cannot be.
CLEARABLE = frozenset(EDITABLE) - {"title", "priority", "difficulty"}

_ALL = frozenset(EDITABLE)
_PRIORITY_ONLY = frozenset({"priority"})
EDITABLE_BY_STATE: dict[TaskState, frozenset[str]] = {
    TaskState.AWAITING_APPROVAL: _ALL,
    TaskState.PENDING: _ALL,
    TaskState.READY: _ALL,
    TaskState.BLOCKED: _PRIORITY_ONLY,
    TaskState.CLAIMED: _PRIORITY_ONLY,
    TaskState.IN_REVIEW: _PRIORITY_ONLY,
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def editable_fields(state: TaskState, *, archived: bool = False) -> list[str]:
    """The fields an edit may change now, in display order."""
    if archived:
        return []
    allowed = EDITABLE_BY_STATE[state]
    return [f for f in EDITABLE if f in allowed]


class TaskChanges(BaseModel):
    """A partial patch: a field left out is preserved, `null` clears (3.2).

    Anything that is not an editable field is kept in `model_extra` so the
    service can refuse it with one structured `immutable_field` error.
    """

    model_config = ConfigDict(extra="allow")

    title: str | None = None
    spec: str | None = None
    acceptance: list[str] | None = None
    priority: int | None = None
    required_capabilities: list[str] | None = None
    knowledge: list[str] | None = None
    depends_on: list[str] | None = None
    difficulty: Difficulty | None = None
    account_providers: list[str] | None = None
    harness_model: HarnessModel | None = None
    executor_model: ExecutorModel | None = None
    harness_account: AccountRef | None = None

    def given(self) -> dict[str, Any]:
        """Editable fields the caller named (even as null), by name."""
        return {f: getattr(self, f) for f in EDITABLE if f in self.model_fields_set}

    def unknown(self) -> list[str]:
        return sorted(self.model_extra or {})


class TaskEdit(BaseModel):
    """The body of `PATCH /api/tasks/{id}` and the arguments of `edit_task`."""

    expected_version: str = Field(min_length=1)  # the task's `version`
    changes: TaskChanges


class TaskAttempt(BaseModel):
    """The definition one attempt was handed at claim (3.5)."""

    attempt: int
    agent_id: str
    claimed_at: datetime
    definition: dict[str, Any]
    # Set only when an operator released the attempt (worker lifecycle 3.3).
    outcome: Literal["released"] | None = None
    outcome_reason: str | None = None
