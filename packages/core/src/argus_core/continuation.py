"""The task continuation contract: what an attempt must know to carry on.

See docs/specs/task-continuation.spec.md. Pure models and functions, no I/O.
Everything here is *Argus-held structure*: free text in it still comes from workers
or orchestrators and is rendered as untrusted data; only enum values and counts
may reach trusted prompt lines.
"""

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argus_core.redact import redact

MAX_ARTIFACTS = 20
MAX_CHECKLIST = 60
MAX_DECISIONS = 30
MAX_ENVIRONMENT = 20
TEXT_MAX = 1000
EVIDENCE_MAX = 2000
_CLARIFICATIONS = "\n## Clarifications\n"


class ContinuationPhase(StrEnum):
    """What this attempt is for."""

    IMPLEMENT = "implement"  # nothing is known to exist yet
    RESUME = "resume"  # continue the branch/PR/WIP that exists
    RECONCILE = "reconcile"  # snapshot and live evidence disagree: settle that first
    QA = "qa"  # implemented (maybe merged): run the pending verification
    CLOSE_EVIDENCE = "close_evidence"  # everything done: gather proof and close


class ContinuationProvenance(StrEnum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker_reported"
    ARGUS = "argus_event"  # derived by Argus from a structured event
    RUNTIME = "runtime_observed"  # the worker runtime looked at the live system


class ArtifactKind(StrEnum):
    BRANCH = "branch"
    PR = "pr"
    COMMIT = "commit"


class ArtifactState(StrEnum):
    UNKNOWN = "unknown"  # never observed: not the same as absent
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"
    LOCAL_ONLY = "local_only"  # exists only in a worker checkout (WIP)


class Tri(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class ItemStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    VERIFICATION_MISSING = "verification_missing"  # claimed done, proof not supplied


class ContinuationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    ref: str = Field(min_length=1, max_length=250)  # branch name, PR number, sha
    url: str | None = Field(default=None, max_length=500)
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    state: ArtifactState = ArtifactState.UNKNOWN
    provenance: ContinuationProvenance
    observed_at: datetime
    attempt: int | None = None
    note: str | None = Field(default=None, max_length=TEXT_MAX)

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind.value, self.ref)


class ChecklistItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=TEXT_MAX)
    status: ItemStatus = ItemStatus.PENDING
    evidence: str | None = Field(default=None, max_length=EVIDENCE_MAX)
    source: Literal["acceptance", "orchestrator"] = "acceptance"
    provenance: ContinuationProvenance = ContinuationProvenance.ARGUS
    updated_at: datetime | None = None


class EffectiveDecision(BaseModel):
    """An answered question as it stands now. History keeps every answer; this is
    the authoritative view: newest first, with what it does and does not settle."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    question: str
    answer: str
    choice: str | None = None
    options_known: bool = False  # the answer picked among options Argus recorded
    category: Literal["decision", "assistance"] = "decision"
    status: Literal["effective", "unresolved", "superseded"] = "effective"
    # Derived on read: it names an artifact that was observed after the answer. The
    # authorization stands; the premise it was given under may no longer hold.
    premise_outdated: bool = False
    scope: Literal["task"] = "task"
    decided_by: str
    decided_at: datetime
    attempt: int | None = None


class ContinuationInterruption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int
    reason: str = Field(
        max_length=100
    )  # released | lease_expired | timeout | fail | ask ...
    detail: str | None = Field(default=None, max_length=TEXT_MAX)
    at: datetime


class ContinuationWip(BaseModel):
    """Work kept outside Git (a local commit, a dirty checkout). Never credentials."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=TEXT_MAX)
    ref: str | None = Field(default=None, max_length=250)  # local sha / path
    provenance: ContinuationProvenance
    observed_at: datetime


class EnvCheck(BaseModel):
    """One resource a verification needs, one dimension per question. `ready` only
    when all five are `yes`: an installed tool without a usable credential, or an
    offline test without the resource, is not readiness."""

    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1, max_length=100)  # postgres, browser, gitea ...
    installed: Tri = Tri.UNKNOWN
    credential_usable: Tri = Tri.UNKNOWN
    reachable: Tri = Tri.UNKNOWN
    permitted: Tri = Tri.UNKNOWN
    validated: Tri = Tri.UNKNOWN  # a real run against the resource succeeded
    provenance: ContinuationProvenance = ContinuationProvenance.RUNTIME
    observed_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return all(
            v is Tri.YES
            for v in (
                self.installed,
                self.credential_usable,
                self.reachable,
                self.permitted,
                self.validated,
            )
        )

    @property
    def missing(self) -> list[str]:
        return [
            name
            for name, v in (
                ("installed", self.installed),
                ("credential_usable", self.credential_usable),
                ("reachable", self.reachable),
                ("permitted", self.permitted),
                ("validated", self.validated),
            )
            if v is not Tri.YES
        ]


class ContinuationReconciliation(BaseModel):
    """A bounded contradiction between recorded and live evidence."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(max_length=300)
    recorded: str = Field(max_length=300)
    observed: str = Field(max_length=300)
    at: datetime


class Continuation(BaseModel):
    """The stored part of the contract. `revision` fences concurrent writers."""

    model_config = ConfigDict(extra="forbid")

    revision: int = 0
    phase: ContinuationPhase | None = None  # explicit; None: derived
    objective: str | None = Field(default=None, max_length=TEXT_MAX)
    artifacts: list[ContinuationArtifact] = Field(
        default_factory=list, max_length=MAX_ARTIFACTS
    )
    wip: ContinuationWip | None = None
    interruption: ContinuationInterruption | None = None
    checklist: list[ChecklistItem] = Field(
        default_factory=list, max_length=MAX_CHECKLIST
    )
    decisions: list[EffectiveDecision] = Field(
        default_factory=list, max_length=MAX_DECISIONS
    )
    environment: list[EnvCheck] = Field(
        default_factory=list, max_length=MAX_ENVIRONMENT
    )
    reconciliation: list[ContinuationReconciliation] = Field(
        default_factory=list, max_length=10
    )


class ChecklistReport(BaseModel):
    """A worker's claim about one item, sent with its result."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    status: ItemStatus
    evidence: str | None = Field(default=None, max_length=EVIDENCE_MAX)


class EffectiveContinuation(BaseModel):
    """What a claim and a read expose: stored facts plus everything derived."""

    model_config = ConfigDict(extra="forbid")

    revision: int
    phase: ContinuationPhase
    phase_source: Literal["explicit", "derived"]
    objective: str | None
    artifacts: list[ContinuationArtifact]
    wip: ContinuationWip | None
    interruption: ContinuationInterruption | None
    checklist: list[ChecklistItem]
    checklist_state: Literal[
        "structured", "unstructured"
    ]  # unstructured: no criteria held
    pending: list[str]  # ids of items not done
    decisions: list[EffectiveDecision]
    environment: list[EnvCheck]
    reconciliation: list[ContinuationReconciliation]
    gaps: list[str]  # what Argus does not know; unknown is not absent


def item_id(text: str) -> str:
    """Stable id of an acceptance item: its text digest, so evidence follows the text."""
    return "ac-" + hashlib.sha256(text.strip().encode()).hexdigest()[:10]


def merge_acceptance(
    stored: list[ChecklistItem], acceptance: list[str]
) -> list[ChecklistItem]:
    """Current acceptance as items, keeping stored evidence for unchanged text.
    Orchestrator-added items always stay. Acceptance edits drop acceptance items whose
    text is gone (the attempt snapshots keep the history)."""
    by_id = {i.id: i for i in stored}
    merged: list[ChecklistItem] = []
    seen: set[str] = set()
    for text in acceptance:
        iid = item_id(text)
        if iid in seen:
            continue
        seen.add(iid)
        merged.append(by_id.get(iid) or ChecklistItem(id=iid, text=text[:TEXT_MAX]))
    merged.extend(i for i in stored if i.source == "orchestrator" and i.id not in seen)
    return merged[:MAX_CHECKLIST]


def split_spec(spec: str) -> tuple[str, str]:
    """(original spec, clarifications section). The section is audit history: the
    structured decisions are authoritative, never this narrative."""
    at = f"\n{spec}".find(_CLARIFICATIONS)  # the prefix lets a leading heading match
    if at < 0:
        return spec, ""
    return spec[: max(at - 1, 0)].rstrip(), spec[
        at:
    ].strip()  # `at` is the heading's index in spec


_SHORT_TOKEN = re.compile(r"^\W*[A-Za-z0-9]{1,3}\W*$")


def classify_answer(
    choice: str | None, text: str, options: list[str] | None
) -> Literal["effective", "unresolved"]:
    """A bare token ("B") with no recorded options authorizes nothing: Argus cannot
    tell what it picked. A choice among recorded options, or a sentence, stands."""
    if options:
        return "effective" if choice in options else "unresolved"
    answer = (text or choice or "").strip()
    return "unresolved" if _SHORT_TOKEN.match(answer) else "effective"


def derive_phase(c: Continuation, checklist: list[ChecklistItem]) -> ContinuationPhase:
    """The attempt's objective when the orchestrator has not set one. Merged is not
    accepted: a merged PR with pending items is QA, not done."""
    if c.phase is not None:
        return c.phase
    if c.reconciliation:
        return ContinuationPhase.RECONCILE
    prs = [a for a in c.artifacts if a.kind is ArtifactKind.PR]
    merged = any(a.state is ArtifactState.MERGED for a in prs)
    pending = [i for i in checklist if i.status is not ItemStatus.DONE]
    if merged and pending:
        return ContinuationPhase.QA
    if merged and checklist:
        return ContinuationPhase.CLOSE_EVIDENCE
    if c.artifacts or c.wip:
        return ContinuationPhase.RESUME
    return ContinuationPhase.IMPLEMENT


def _mentions(text: str, ref: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(ref)}(?![\w])", text) is not None


def _mark_outdated(
    d: EffectiveDecision, artifacts: list[ContinuationArtifact]
) -> EffectiveDecision:
    said = f"{d.question} {d.answer}"
    outdated = any(
        a.observed_at > d.decided_at
        and a.state is not ArtifactState.UNKNOWN
        and a.provenance is not ContinuationProvenance.WORKER
        and _mentions(said, a.ref)
        for a in artifacts
    )
    return d.model_copy(update={"premise_outdated": outdated}) if outdated else d


def effective(
    c: Continuation, acceptance: list[str], *, legacy_spec: bool = False
) -> EffectiveContinuation:
    checklist = merge_acceptance(c.checklist, acceptance)
    structured = bool(checklist)
    gaps: list[str] = []
    if not structured:
        gaps.append("acceptance_not_structured")
    if not c.artifacts:
        gaps.append("artifacts_unknown")  # none recorded; existence is not excluded
    elif all(a.state is ArtifactState.UNKNOWN for a in c.artifacts):
        gaps.append("artifact_state_unobserved")
    gaps += [
        f"environment_not_ready:{e.resource}" for e in c.environment if not e.ready
    ]
    gaps += [
        f"decision_unresolved:{d.decision_id}"
        for d in c.decisions
        if d.status == "unresolved"
    ]
    decisions = sorted(
        (_mark_outdated(d, c.artifacts) for d in c.decisions),
        key=lambda d: d.decided_at,
        reverse=True,
    )
    return EffectiveContinuation(
        revision=c.revision,
        phase=derive_phase(c, checklist),
        phase_source="explicit" if c.phase is not None else "derived",
        objective=c.objective,
        artifacts=c.artifacts,
        wip=c.wip,
        interruption=c.interruption,
        checklist=checklist,
        checklist_state="structured" if structured else "unstructured",
        pending=[i.id for i in checklist if i.status is not ItemStatus.DONE],
        decisions=decisions,
        environment=c.environment,
        reconciliation=c.reconciliation,
        gaps=gaps,
    )


def clean(text: str | None, limit: int = TEXT_MAX) -> str | None:
    """Redacted, single-pass bounded free text for storage (no secrets, no growth)."""
    if text is None:
        return None
    return redact(text)[:limit]


# -- request bodies (shared by REST and MCP) -----------------------------------------


class ArtifactIn(BaseModel):
    """A caller's statement about an artifact. ContinuationProvenance and time are Argus's."""

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    ref: str = Field(min_length=1, max_length=250)
    url: str | None = Field(default=None, max_length=500)
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    state: ArtifactState = ArtifactState.UNKNOWN
    note: str | None = Field(default=None, max_length=TEXT_MAX)


class WipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=TEXT_MAX)
    ref: str | None = Field(default=None, max_length=250)


class EnvIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1, max_length=100)
    installed: Tri = Tri.UNKNOWN
    credential_usable: Tri = Tri.UNKNOWN
    reachable: Tri = Tri.UNKNOWN
    permitted: Tri = Tri.UNKNOWN
    validated: Tri = Tri.UNKNOWN


class ObservationRequest(BaseModel):
    """What a worker runtime saw (by itself, outside any transaction) at claim."""

    model_config = ConfigDict(extra="forbid")

    attempt: int
    artifacts: list[ArtifactIn] = Field(default_factory=list, max_length=MAX_ARTIFACTS)
    environment: list[EnvIn] = Field(default_factory=list, max_length=MAX_ENVIRONMENT)
    wip: WipIn | None = None


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=64)  # omitted: a new item
    text: str | None = Field(default=None, min_length=1, max_length=TEXT_MAX)
    status: ItemStatus | None = None
    evidence: str | None = Field(default=None, max_length=EVIDENCE_MAX)


class ContinuationUpdate(BaseModel):
    """An orchestrator/admin edit. Everything it writes carries `orchestrator`
    provenance; `expected_revision` rejects a write based on a stale read."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    reason: str = Field(min_length=1, max_length=TEXT_MAX)
    phase: ContinuationPhase | None = None
    clear_phase: bool = False
    objective: str | None = Field(default=None, max_length=TEXT_MAX)
    artifacts: list[ArtifactIn] = Field(default_factory=list, max_length=MAX_ARTIFACTS)
    wip: WipIn | None = None
    clear_wip: bool = False
    checklist: list[ItemIn] = Field(default_factory=list, max_length=MAX_CHECKLIST)
    environment: list[EnvIn] = Field(default_factory=list, max_length=MAX_ENVIRONMENT)
    supersede_decisions: list[str] = Field(
        default_factory=list, max_length=MAX_DECISIONS
    )
    clear_reconciliation: bool = False
