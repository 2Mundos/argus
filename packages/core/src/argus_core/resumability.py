"""Read-only task resumability preflight: models and pure classification.

See docs/specs/task-resumability-preflight.spec.md. No I/O. Every claim carries
exactly one evidence class: recorded metadata and historical receipts never
imply current availability, and a preflight proves nothing about a different
attempt than the one it describes.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ARTIFACTS = 20
MAX_INPUTS = 20
MAX_REASONS = 10
MAX_NEXT_STEPS = 5
MAX_DECISION_CHANGES = 20
MAX_UNKNOWNS = 20
TEXT_MAX = 300
DETAIL_MAX = 300

# Default bound for a current-read hash of one attachment (bytes). Larger files
# are classified `unknown` without reading past the bound.
DEFAULT_READ_BOUND = 32 * 1024 * 1024


class EvidenceClass(StrEnum):
    """How a claim was established (spec 3.3)."""

    RECORDED_METADATA = "recorded_metadata"  # Argus stored it
    RECEIPT_ONLY = "receipt_only"  # historical task.input receipt; not current
    CURRENT_READ = "current_read"  # this request re-read size and SHA-256
    UNOBSERVED = "unobserved"  # no observation
    UNKNOWN = "unknown"  # could not complete a current read


class Availability(StrEnum):
    """Current store state for an attachment (spec 3.4)."""

    AVAILABLE = "available"
    MISSING = "missing"
    TRUNCATED = "truncated"
    CORRUPTED = "corrupted"
    INACCESSIBLE = "inaccessible"
    UNKNOWN = "unknown"


class ResumabilityDecision(StrEnum):
    RESUMABLE = "resumable"
    NOT_RESUMABLE = "not_resumable"
    UNKNOWN = "unknown"
    STALE = "stale"


class ResumabilityCause(StrEnum):
    """Closed set of cause codes (spec 3.8)."""

    TASK_UNKNOWN = "task_unknown"
    TASK_TERMINAL = "task_terminal"
    ATTEMPT_MISMATCH = "attempt_mismatch"
    STALE_PREFLIGHT = "stale_preflight"
    NO_ARTIFACTS = "no_artifacts"
    ARTIFACT_STATE_UNOBSERVED = "artifact_state_unobserved"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_TRUNCATED = "artifact_truncated"
    ARTIFACT_CORRUPTED = "artifact_corrupted"
    ARTIFACT_INACCESSIBLE = "artifact_inaccessible"
    ARTIFACT_UNKNOWN = "artifact_unknown"
    WIP_LOCAL_ONLY = "wip_local_only"
    CHECKPOINT_STORE_NOT_VISIBLE = "checkpoint_store_not_visible"
    WIP_UNRECORDED = "wip_unrecorded"
    INPUT_CAPABILITY_MISSING = "input_capability_missing"
    DECISION_UNRESOLVED = "decision_unresolved"
    ACCEPTANCE_NOT_STRUCTURED = "acceptance_not_structured"
    LEASE_EXPIRED = "lease_expired"
    LEASE_HELD_ELSEWHERE = "lease_held_elsewhere"


class ConsumerPath(StrEnum):
    """The only supported cross-attempt consumer for attachment bytes."""

    TASK_INPUTS_MATERIALIZER = "task_inputs_materializer"
    NONE = "none"


class WipKind(StrEnum):
    PUBLISHED = "published"
    LOCAL_ONLY = "local_only"
    UNKNOWN = "unknown"
    ABSENT = "absent"


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ResumabilityCause
    subject: str = Field(max_length=TEXT_MAX)
    evidence_class: EvidenceClass
    detail: str | None = Field(default=None, max_length=DETAIL_MAX)


class ArtifactAssessment(BaseModel):
    """One artifact as assessed: recorded facts plus optional current-read."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=64)  # branch|pr|commit|attachment|…
    ref: str = Field(max_length=TEXT_MAX)
    artifact_id: str | None = Field(default=None, max_length=64)
    state: str | None = Field(default=None, max_length=64)
    provenance: str | None = Field(default=None, max_length=64)
    availability: Availability
    evidence_class: EvidenceClass
    recorded_size: int | None = None
    recorded_sha256: str | None = Field(default=None, max_length=64)
    observed_size: int | None = None
    observed_sha256: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=TEXT_MAX)


class InputReceiptView(BaseModel):
    """A historical receipt: evidence_class is always receipt_only."""

    model_config = ConfigDict(extra="forbid")

    attempt: int
    state: str = Field(max_length=32)
    reason: str | None = Field(default=None, max_length=64)
    evidence_class: Literal[EvidenceClass.RECEIPT_ONLY] = EvidenceClass.RECEIPT_ONLY


class InputAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(max_length=64)
    name: str = Field(max_length=200)
    recorded_size: int
    recorded_sha256: str = Field(max_length=64)
    availability: Availability
    evidence_class: EvidenceClass
    receipts: list[InputReceiptView] = Field(default_factory=list, max_length=20)
    consumer_path: ConsumerPath
    capability: Literal["required", "capability_not_required", "capability_unknown"] = (
        "capability_unknown"
    )
    # True only when continuation WIP names this attachment
    # (`attachment:<id>:<sha256>`). Optional shared inputs are still assessed but
    # never flip `required_inputs_ok` / the top-level decision.
    required: bool = False


class WipAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: WipKind
    ref: str | None = Field(default=None, max_length=TEXT_MAX)
    summary: str | None = Field(default=None, max_length=TEXT_MAX)
    availability: Availability | None = None
    evidence_class: EvidenceClass
    transfer: (
        Literal["not_a_transfer", "attachment_verified", "published_ref"] | None
    ) = None


class LeaseAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_agent_id: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    live: bool
    evidence_class: EvidenceClass = EvidenceClass.RECORDED_METADATA


class DecisionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(max_length=64)
    status: str = Field(max_length=32)
    question: str = Field(max_length=TEXT_MAX)
    decided_at: datetime | None = None
    premise_outdated: bool = False
    relative_to_attempt: Literal["at_or_after", "before", "unresolved"]


class NextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=64)
    detail: str = Field(max_length=DETAIL_MAX)


class ResumabilityReport(BaseModel):
    """Bounded read-only assessment (spec 3.2). Grants nothing."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    attempt: int
    task_version: str
    assessed_at: datetime
    decision: ResumabilityDecision
    cause_codes: list[ResumabilityCause] = Field(default_factory=list, max_length=20)
    reasons: list[Reason] = Field(default_factory=list, max_length=MAX_REASONS)
    artifacts: list[ArtifactAssessment] = Field(
        default_factory=list, max_length=MAX_ARTIFACTS
    )
    wip: WipAssessment
    inputs: list[InputAssessment] = Field(default_factory=list, max_length=MAX_INPUTS)
    lease: LeaseAssessment
    decision_changes: list[DecisionChange] = Field(
        default_factory=list, max_length=MAX_DECISION_CHANGES
    )
    unknowns: list[str] = Field(default_factory=list, max_length=MAX_UNKNOWNS)
    next_steps: list[NextStep] = Field(default_factory=list, max_length=MAX_NEXT_STEPS)


def classify_artifact(
    *,
    recorded_size: int | None,
    recorded_sha256: str | None,
    observed_size: int | None,
    observed_sha256: str | None,
    error: Literal[
        "missing", "permission", "os_error", "is_directory", "too_large", "other"
    ]
    | None = None,
) -> tuple[Availability, EvidenceClass, ResumabilityCause | None]:
    """Map a current-read (or its failure) to availability + evidence + optional cause.

    `observed_*` are set only when bytes were read; `error` covers the distinct
    failure modes of the store read. Remote (non-attachment) artifacts are not
    classified here — callers assign `unknown` / `recorded_metadata` themselves.
    """
    if error == "missing":
        return (
            Availability.MISSING,
            EvidenceClass.CURRENT_READ,
            ResumabilityCause.ARTIFACT_MISSING,
        )
    if error == "permission":
        return (
            Availability.INACCESSIBLE,
            EvidenceClass.CURRENT_READ,
            ResumabilityCause.ARTIFACT_INACCESSIBLE,
        )
    if error in ("os_error", "is_directory"):
        return (
            Availability.INACCESSIBLE,
            EvidenceClass.CURRENT_READ,
            ResumabilityCause.ARTIFACT_INACCESSIBLE,
        )
    if error == "too_large":
        return (
            Availability.UNKNOWN,
            EvidenceClass.UNKNOWN,
            ResumabilityCause.ARTIFACT_UNKNOWN,
        )
    if error == "other" or recorded_size is None or recorded_sha256 is None:
        return (
            Availability.UNKNOWN,
            EvidenceClass.UNKNOWN,
            ResumabilityCause.ARTIFACT_UNKNOWN,
        )
    if observed_size is None:
        return (
            Availability.MISSING,
            EvidenceClass.CURRENT_READ,
            ResumabilityCause.ARTIFACT_MISSING,
        )
    if observed_size < recorded_size:
        return (
            Availability.TRUNCATED,
            EvidenceClass.CURRENT_READ,
            ResumabilityCause.ARTIFACT_TRUNCATED,
        )
    if observed_size > recorded_size:
        return (
            Availability.CORRUPTED,
            EvidenceClass.CURRENT_READ,
            ResumabilityCause.ARTIFACT_CORRUPTED,
        )
    if observed_sha256 != recorded_sha256:
        return (
            Availability.CORRUPTED,
            EvidenceClass.CURRENT_READ,
            ResumabilityCause.ARTIFACT_CORRUPTED,
        )
    return Availability.AVAILABLE, EvidenceClass.CURRENT_READ, None


# Bare attachment availability codes (missing/truncated/corrupted/inaccessible)
# are never deciding by themselves: an unavailable *optional* input is reported
# as a cause and a reason, never as a block. `required_inputs_ok` already
# reflects only the required WIP attachment (if any).
_NOT_RESUMABLE = frozenset(
    {
        ResumabilityCause.TASK_TERMINAL,
        ResumabilityCause.ARTIFACT_UNAVAILABLE,
        ResumabilityCause.INPUT_CAPABILITY_MISSING,
    }
)
_STALE = frozenset(
    {
        ResumabilityCause.ATTEMPT_MISMATCH,
        ResumabilityCause.STALE_PREFLIGHT,
    }
)
_UNKNOWN = frozenset(
    {
        ResumabilityCause.TASK_UNKNOWN,
        ResumabilityCause.ARTIFACT_STATE_UNOBSERVED,
        ResumabilityCause.ARTIFACT_UNKNOWN,
        ResumabilityCause.CHECKPOINT_STORE_NOT_VISIBLE,
        ResumabilityCause.WIP_UNRECORDED,
        ResumabilityCause.DECISION_UNRESOLVED,
        ResumabilityCause.ACCEPTANCE_NOT_STRUCTURED,
        ResumabilityCause.NO_ARTIFACTS,
        ResumabilityCause.WIP_LOCAL_ONLY,
        ResumabilityCause.LEASE_EXPIRED,
        ResumabilityCause.LEASE_HELD_ELSEWHERE,
    }
)


def derive_decision(
    causes: list[ResumabilityCause],
    *,
    required_inputs_ok: bool,
    required_unobserved: bool,
) -> ResumabilityDecision:
    """Map cause codes and required-input status to the top-level decision.

    `resumable` only when nothing blocks and every *required* input was a
    successful current read. Stale beats everything else when the caller's
    premise mismatches.

    Attachment availability codes (missing / truncated / corrupted /
    inaccessible) do not decide `not_resumable` by themselves: that follows
    `required_inputs_ok`, which only reflects the required WIP attachment ref
    (if any). An unavailable optional input is reported as a cause and a
    reason, never as a block. `task_terminal`, `artifact_unavailable` and
    `input_capability_missing` remain `not_resumable`.
    """
    codes = set(causes)
    if codes & _STALE:
        return ResumabilityDecision.STALE
    if codes & _NOT_RESUMABLE or not required_inputs_ok:
        return ResumabilityDecision.NOT_RESUMABLE
    if required_unobserved or codes & _UNKNOWN:
        return ResumabilityDecision.UNKNOWN
    return ResumabilityDecision.RESUMABLE
