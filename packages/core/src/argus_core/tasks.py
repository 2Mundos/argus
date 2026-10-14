"""Task states, the transition function, and task models.

See docs/specs/worker-lifecycle.spec.md 3.2.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from argus_core.knowledge import KnowledgeRef


class TaskState(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    PENDING = "pending"
    READY = "ready"
    CLAIMED = "claimed"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})


class Action(StrEnum):
    APPROVE = "approve"
    DEPS_SATISFIED = "deps_satisfied"
    CLAIM = "claim"
    COMPLETE = "complete"
    FAIL = "fail"
    EXPIRE = "expire"
    BLOCK = "block"
    UNBLOCK = "unblock"
    ACCEPT = "accept"
    REJECT = "reject"
    CANCEL = "cancel"


class Review(StrEnum):
    NONE = "none"
    HUMAN = "human"
    ORCHESTRATOR = "orchestrator"


@dataclass(frozen=True)
class TransitionContext:
    """Task facts some transitions depend on."""

    failures: int = 0  # failures so far, before this action
    max_attempts: int = 3
    review: Review = Review.NONE
    deps_done: bool = True


class InvalidTransitionError(Exception):
    def __init__(self, state: TaskState, action: Action) -> None:
        super().__init__(f"cannot {action} a task that is {state}")
        self.state = state
        self.action = action


def transition(state: TaskState, action: Action, ctx: TransitionContext) -> TaskState:
    """Return the state after `action`, or raise InvalidTransitionError."""
    S, A = TaskState, Action
    if action is A.CANCEL and state not in TERMINAL:
        return S.CANCELLED
    match state, action:
        case S.AWAITING_APPROVAL, A.APPROVE:
            return S.READY if ctx.deps_done else S.PENDING
        case S.PENDING, A.DEPS_SATISFIED:
            return S.READY
        case S.READY, A.CLAIM:
            return S.CLAIMED
        case S.CLAIMED, A.COMPLETE:
            return S.SUCCEEDED if ctx.review is Review.NONE else S.IN_REVIEW
        case S.CLAIMED, A.FAIL | A.EXPIRE:
            return S.READY if ctx.failures + 1 < ctx.max_attempts else S.FAILED
        case S.CLAIMED, A.BLOCK:
            return S.BLOCKED
        case S.BLOCKED, A.UNBLOCK:
            return S.READY
        case S.IN_REVIEW, A.ACCEPT:
            return S.SUCCEEDED
        case S.IN_REVIEW, A.REJECT:
            return S.READY
    raise InvalidTransitionError(state, action)


class Usage(BaseModel):
    """What the work cost, as reported by the agent. Recorded, never enforced."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    account_id: str | None = None


class TaskResult(BaseModel):
    """The one result envelope every task completes with (3.11)."""

    summary: str = Field(min_length=1, max_length=10_000)
    data: dict[str, Any] | None = None
    artifacts: list[str] = []  # artifact ids uploaded for this task
    usage: Usage | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    spec: str = ""
    kind: str = "generic"
    acceptance: list[str] = []
    input: dict[str, Any] = {}
    required_capabilities: list[str] = []
    priority: int = 0
    requires_approval: bool = False
    review: Review = Review.NONE
    parent_id: str | None = None
    depends_on: list[str] = []
    # Required; a subtask may omit it to inherit its parent's project.
    project: str | None = None
    knowledge: list[str] = []  # page ids pinned to the task, delivered on claim
    max_attempts: int = Field(default=3, ge=1, le=20)
    lease_ttl_s: int = Field(default=120, ge=10, le=3600)


class Task(BaseModel):
    id: str
    parent_id: str | None
    kind: str
    title: str
    spec: str
    acceptance: list[str]
    input: dict[str, Any]
    required_capabilities: list[str]
    priority: int
    state: TaskState
    requires_approval: bool
    review: Review
    assignee_agent_id: str | None
    lease_expires_at: datetime | None
    lease_ttl_s: int
    attempt: int
    failures: int
    max_attempts: int
    result: TaskResult | None
    error: str | None
    block_reason: str | None
    progress_message: str | None
    progress_percent: int | None
    progress_at: datetime | None
    depends_on: list[str]
    project: str
    knowledge: list[str]
    created_by: str
    created_at: datetime
    updated_at: datetime


class ClaimedTask(Task):
    """A claimed task plus knowledge worth reading first: pinned pages, then the
    best matches for the task in its project (references only)."""

    knowledge_refs: list[KnowledgeRef] = []
