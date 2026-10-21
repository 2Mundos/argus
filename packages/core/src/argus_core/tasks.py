"""Task states, the transition function, and task models.

See docs/specs/worker-lifecycle.spec.md 3.2.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from argus_core.accounts import ENGINE_PROVIDERS
from argus_core.difficulty import Difficulty, DifficultySource
from argus_core.knowledge import KnowledgeRef
from argus_core.model_trace import ModelsSummary
from argus_core.quota import FailureClass


def validate_account_providers(value: list[str] | None) -> list[str] | None:
    """A task's provider restriction: None, or a non-empty list of engine-capable
    providers (never `gitea`, never an unknown one), de-duplicated and sorted."""
    if value is None:
        return None
    if not value:
        raise ValueError("must be non-empty; use null for no restriction")
    if bad := sorted(set(value) - ENGINE_PROVIDERS):
        raise ValueError(
            f"not engine providers: {', '.join(bad)}"
            f" (known: {', '.join(sorted(ENGINE_PROVIDERS))})"
        )
    return sorted(set(value))


# The account provider a pinned engine or executor needs (task models spec 3.1).
HARNESS_PROVIDERS: dict[str, str] = {"claude": "anthropic", "codex": "openai"}
EXECUTOR_PROVIDERS: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "agent": "cursor",
}
ModelId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
]


class HarnessModel(BaseModel):
    """A task's pinned harness engine and model (task models spec)."""

    model_config = ConfigDict(extra="forbid")

    engine: Literal["claude", "codex"]
    model: ModelId

    @property
    def provider(self) -> str:
        return HARNESS_PROVIDERS[self.engine]


class ExecutorModel(BaseModel):
    """A task's pinned delegated executor and model (task models spec)."""

    model_config = ConfigDict(extra="forbid")

    executor: Literal["claude", "codex", "agent"]
    model: ModelId

    @property
    def provider(self) -> str:
        return EXECUTOR_PROVIDERS[self.executor]


def check_harness_providers(
    harness: HarnessModel | None, account_providers: list[str] | None
) -> str | None:
    """Why a harness pin cannot run under the task's provider restriction, or None."""
    if harness is None or account_providers is None:
        return None
    if harness.provider in account_providers:
        return None
    return (
        f"engine {harness.engine} needs provider {harness.provider},"
        " excluded by account_providers"
    )


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


class ProgressKind(StrEnum):
    """The closed set of structured progress milestones (progress-milestones spec 3.1)."""

    COMMIT_CREATED = "commit_created"
    PR_OPENED = "pr_opened"
    CI_RUNNING = "ci_running"
    CI_PASSED = "ci_passed"
    CI_FAILED = "ci_failed"
    GATE_FAILED = "gate_failed"
    WAITING_INPUT = "waiting_input"


_SHA = re.compile(r"[0-9a-f]{7,40}")
_MILESTONE_KEYS: dict[ProgressKind, frozenset[str]] = {
    ProgressKind.COMMIT_CREATED: frozenset({"branch", "sha"}),
    ProgressKind.PR_OPENED: frozenset({"number", "url"}),
    ProgressKind.CI_RUNNING: frozenset({"sha"}),
    ProgressKind.CI_PASSED: frozenset({"sha"}),
    ProgressKind.CI_FAILED: frozenset({"sha"}),
    ProgressKind.GATE_FAILED: frozenset({"gate"}),
    ProgressKind.WAITING_INPUT: frozenset(),
}


def _text(data: dict[str, Any], key: str, limit: int) -> str:
    value = data[key]
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError(f"data.{key} must be a string of 1-{limit} characters")
    return value


def validate_milestone(kind: str | None, data: dict[str, Any] | None) -> None:
    """Raise `ValueError` unless `kind` is a known milestone and `data` has exactly its shape."""
    if kind is None:
        if data:
            raise ValueError("data requires kind")
        return
    try:
        k = ProgressKind(kind)
    except ValueError:
        allowed = ", ".join(m.value for m in ProgressKind)
        raise ValueError(f"unknown kind {kind!r}; one of {allowed}") from None
    data = data or {}
    keys = _MILESTONE_KEYS[k]
    if set(data) != keys:
        raise ValueError(
            f"{k.value} data must have exactly: {sorted(keys) or 'no keys'}"
        )
    if "sha" in keys and not (
        isinstance(data["sha"], str) and _SHA.fullmatch(data["sha"])
    ):
        raise ValueError("data.sha must be 7-40 lowercase hex characters")
    if k is ProgressKind.COMMIT_CREATED:
        _text(data, "branch", 250)
    if k is ProgressKind.GATE_FAILED:
        _text(data, "gate", 200)
    if k is ProgressKind.PR_OPENED:
        n = data["number"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError("data.number must be a positive integer")
        url = _text(data, "url", 500)
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "data.url must be an http(s) URL without userinfo, query or fragment"
            )


TERMINAL = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})


class Action(StrEnum):
    APPROVE = "approve"
    DEPS_SATISFIED = "deps_satisfied"
    DEPS_ADDED = "deps_added"  # an unfinished dependency was added to a ready task
    CLAIM = "claim"
    COMPLETE = "complete"
    FAIL = "fail"
    EXPIRE = "expire"
    BLOCK = "block"
    REQUEUE = "requeue"  # a provider quota ended the attempt: ready again, no failure
    RELEASE = "release"  # an operator took the claim back: ready again, no failure
    UNBLOCK = "unblock"
    ACCEPT = "accept"
    REJECT = "reject"
    CANCEL = "cancel"


class Review(StrEnum):
    NONE = "none"
    HUMAN = "human"
    ORCHESTRATOR = "orchestrator"


class PromptTrust(StrEnum):
    """Immutable origin of task content for prompt isolation.

    Orchestrator/admin-created tasks are `orchestrator`. Worker-created subtasks
    are always `worker` and never elevate, even under an orchestrator parent.
    Spec/acceptance/knowledge remain untrusted DATA in the worker prompt either way.
    """

    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"


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
        case S.READY, A.DEPS_ADDED:
            return S.PENDING
        case S.READY, A.CLAIM:
            return S.CLAIMED
        case S.CLAIMED, A.COMPLETE:
            return S.SUCCEEDED if ctx.review is Review.NONE else S.IN_REVIEW
        case S.CLAIMED, A.FAIL | A.EXPIRE:
            return S.READY if ctx.failures + 1 < ctx.max_attempts else S.FAILED
        case S.CLAIMED, A.REQUEUE | A.RELEASE:
            return S.READY
        case S.CLAIMED | S.READY, A.BLOCK:  # a ready task may wait for quota (3.9)
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
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=100)
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
    # Wall-clock cap per attempt, counted from the claim; heartbeats and progress
    # notes do not extend it. Unset: the kind's default, then the server's.
    timeout_s: int | None = Field(default=None, ge=60, le=7 * 24 * 3600)
    # Unset: derived from the title and spec (task difficulty spec). The source
    # defaults to `declared`; an orchestrator suggesting a level sends `inferred`.
    difficulty: Difficulty | None = None
    difficulty_source: DifficultySource | None = None
    # Restrict the task to these engine account providers (e.g. ["local"]); None:
    # unrestricted (local harness spec 3).
    account_providers: list[str] | None = None
    # Pinned engine/executor and model; None: policy and account priority.
    harness_model: HarnessModel | None = None
    executor_model: ExecutorModel | None = None

    @field_validator("account_providers")
    @classmethod
    def _engine_providers(cls, value: list[str] | None) -> list[str] | None:
        return validate_account_providers(value)

    @model_validator(mode="after")
    def _harness_allowed(self) -> "TaskCreate":
        if why := check_harness_providers(self.harness_model, self.account_providers):
            raise ValueError(f"harness_model: {why}")
        return self


class Failover(BaseModel):
    """The last switch of the account serving a task (account selection 3.6)."""

    from_account: str | None  # account names, never credentials
    to_account: str | None  # None: nothing usable was left (awaiting quota)
    reason: str  # sanitized: one line, no tokens or query strings
    at: datetime
    attempt: int
    reason_code: Literal["quota_exhausted", "auth_expired"] | None = None
    reset_status: Literal["stated", "ambiguous", "absent"] | None = None
    reset_text: str | None = None
    reset_at: datetime | None = None
    source: Literal["structured", "notice", "log"] | None = None
    outcome: Literal["switched", "awaiting_quota", "switch_failed"] | None = None
    apply: Literal["pending", "applied", "not_needed", "failed"] | None = None
    apply_error: str | None = None
    applied_at: datetime | None = None


class ServingAccount(BaseModel):
    id: str
    name: str
    provider: str
    # Custom accounts only (custom accounts spec 3): the configured engine model and
    # the host calls go to. Not provider-observed: the model actually served may differ.
    model: str | None = None
    host: str | None = None


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
    prompt_trust: PromptTrust
    assignee_agent_id: str | None
    lease_expires_at: datetime | None
    lease_ttl_s: int
    timeout_s: int | None
    timeout_at: datetime | None  # this attempt's deadline while claimed
    attempt: int
    failures: int
    max_attempts: int
    result: TaskResult | None
    error: str | None
    block_reason: str | None
    failure_class: FailureClass | None = None
    progress_message: str | None
    progress_percent: int | None
    progress_at: datetime | None
    progress_kind: ProgressKind | None = None
    progress_data: dict[str, Any] | None = None
    output_at: datetime | None = (
        None  # last accepted live-log batch (task-live-log spec)
    )
    depends_on: list[str]
    project: str
    knowledge: list[str]
    difficulty: Difficulty = Difficulty.STANDARD
    difficulty_source: DifficultySource = DifficultySource.INFERRED
    account_providers: list[str] | None = None
    harness_model: HarnessModel | None = None
    executor_model: ExecutorModel | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None  # set while an operator has archived it
    version: str  # updated_at; pass it back as expected_version
    # The fields an edit may change in this state (docs/specs/task-edit.spec.md 3.4).
    editable_fields: list[str] = []
    # Derived on read, never stored: when the earliest exhausted account is usable
    # again (blocked as awaiting_quota), the account running the current attempt,
    # and the last account switch.
    refill_at: datetime | None = None
    serving_account: ServingAccount | None = None
    failover: Failover | None = None
    # Models per layer (model observability 3.6): derived, never stored here.
    models: ModelsSummary = ModelsSummary()

    @computed_field  # deprecated: read `difficulty`
    @property
    def complexity(self) -> Literal["trivial", "standard", "hard"]:
        """The pre-difficulty three-level view, for clients that still read it:
        `simple` is `trivial` there, as both prefer the cheap executor."""
        if self.difficulty is Difficulty.SIMPLE:
            return "trivial"
        return self.difficulty.value  # type: ignore[return-value]


class ClaimedTask(Task):
    """A claimed task plus knowledge worth reading first: pinned pages, then the
    best matches for the task in its project (references only)."""

    knowledge_refs: list[KnowledgeRef] = []
