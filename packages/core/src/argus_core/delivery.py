"""A task's delivery as the forge shows it (docs/specs/task-delivery.spec.md)."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PrState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"
    UNKNOWN = "unknown"  # not read (no credential, network, auth): never "open"


class CheckState(StrEnum):
    SUCCESS = "success"
    PENDING = "pending"
    FAILURE = "failure"
    ERROR = "error"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class FactOrigin(StrEnum):
    FORGE = "forge"
    TASK = "task"
    UNKNOWN = "unknown"


AcceptBlocked = Literal["not_in_review", "checklist_pending", "no_review"]
MERGED_PENDING_REVIEW = "delivery_merged_pending_review"

AUTH_NOTES = (
    "Green checks do not authorize merge. Merge is not acceptance. "
    "Authorization and checklist are separate from forge CI."
)


class PrBinding(BaseModel):
    """Bind a pull request the worker opened but never reported to its task (task
    deliverable spec 3.7). The head is the exact one the caller expects the forge to
    show; anything else is refused."""

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=500)  # https://host/owner/repo
    number: int = Field(ge=1, le=999_999_999)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    reason: str = Field(min_length=1, max_length=1000)


class DeliveryReconciliation(PrBinding):
    """Move a task that failed `deliverable_missing` to review because its bound PR
    merged (task deliverable spec 3.8). `attempt` and `failure_event_id` name the
    failure the caller looked at; a newer one makes the request stale."""

    attempt: int = Field(ge=0)
    failure_event_id: int = Field(ge=1)


class DeliveryTask(BaseModel):
    id: str
    state: str
    project: str
    source: Literal["task"] = "task"


class ShaFact(BaseModel):
    sha: str | None = None
    ref: str | None = None
    source: FactOrigin = FactOrigin.UNKNOWN
    checked_at: datetime | None = None


class MergeFact(BaseModel):
    mergeable: bool | None = None
    mergeable_state: str | None = None
    source: FactOrigin = FactOrigin.UNKNOWN
    checked_at: datetime | None = None


class DeliveryCheck(BaseModel):
    name: str
    state: CheckState
    kind: Literal["status", "check_run"]
    sha: str
    source: Literal["forge"] = "forge"
    required: bool | None = None  # null when required gates are unknown
    description: str | None = None
    target_url: str | None = None
    checked_at: datetime


class RequiredGates(BaseModel):
    """Present only when the forge disclosed required contexts (may be empty)."""

    contexts: list[str]
    source: Literal["forge"] = "forge"
    checked_at: datetime


class IndependentReviewSummary(BaseModel):
    child_task_id: str | None = None
    verdict: str | None = None  # approve | request_changes | pending | unknown
    head_sha: str | None = None
    source: Literal["task"] = "task"


class DeliveryReviewEvidence(BaseModel):
    independent: IndependentReviewSummary | None = None
    forge_review_count: int | None = None
    forge_reviews_for_head: int | None = None
    agentic_finding_count: int | None = None  # null = unknown; never invent 0
    sha: str | None = None
    source: FactOrigin = FactOrigin.UNKNOWN
    checked_at: datetime | None = None


class DeliveryChecklist(BaseModel):
    pending: int
    done: int
    total: int
    pending_ids: list[str] = Field(default_factory=list)
    source: Literal["task"] = "task"


class DeliveryAuthorization(BaseModel):
    allow_merge: bool | None = None  # true only when input.delivery.allow_merge is true
    accept_decision_id: str | None = None
    accept_blocked: AcceptBlocked | None = None
    notes: str = AUTH_NOTES
    source: Literal["task"] = "task"


class Delivery(BaseModel):
    """What Argus read of the task's pull request. Observed only: a merge neither
    closes the task nor counts as acceptance. Green checks do not authorize merge."""

    pr: str  # canonical URL of the pull request
    pr_state: PrState
    merged_at: datetime | None
    checked_at: datetime
    flags: list[Literal["delivery_merged_pending_review"]]
    # The existing review decision, when the state and checklist allow accepting it.
    accept_decision_id: str | None
    # Why accepting is not offered although the PR is merged.
    accept_blocked: AcceptBlocked | None
    # Additive projection (task-delivery.spec.md 3.3); old clients ignore these.
    task: DeliveryTask | None = None
    head: ShaFact | None = None
    base: ShaFact | None = None
    merge: MergeFact | None = None
    checks: list[DeliveryCheck] = Field(default_factory=list)
    checks_combined: CheckState | None = None
    required_gates: RequiredGates | None = None
    review: DeliveryReviewEvidence | None = None
    checklist: DeliveryChecklist | None = None
    authorization: DeliveryAuthorization | None = None
    stale: bool = False
    partial: list[str] = Field(default_factory=list)
    disclosures: list[str] = Field(default_factory=list)
