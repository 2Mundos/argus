"""Read-only task resumability preflight (docs/specs/task-resumability-preflight.spec.md).

Assesses attempt/lease state, WIP, and attachment availability. Never writes.
Attachment hashing reuses the local artifact store path/key and runs outside any
write transaction. Recorded metadata and receipts never imply current availability.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Literal

import aiosqlite
import anyio
from argus_core.continuation import (
    ArtifactState,
    ContinuationProvenance,
    EffectiveContinuation,
)
from argus_core.redact import redact
from argus_core.resumability import (
    DEFAULT_READ_BOUND,
    DETAIL_MAX,
    MAX_ARTIFACTS,
    MAX_DECISION_CHANGES,
    MAX_INPUTS,
    MAX_NEXT_STEPS,
    MAX_REASONS,
    MAX_UNKNOWNS,
    TEXT_MAX,
    ArtifactAssessment,
    Availability,
    ConsumerPath,
    DecisionChange,
    EvidenceClass,
    InputAssessment,
    InputReceiptView,
    LeaseAssessment,
    NextStep,
    Reason,
    ResumabilityCause,
    ResumabilityDecision,
    ResumabilityReport,
    WipAssessment,
    WipKind,
    classify_artifact,
    derive_decision,
)
from argus_core.tasks import TERMINAL, Task

from argus_api.artifacts import inputs as inputs_mod
from argus_api.artifacts.service import (
    Artifact,
    ArtifactSource,
    _path,
    get_artifact,
)
from argus_api.artifacts.store import LocalArtifactStore
from argus_api.tasks.continuation import INPUTS_CAPABILITY

_CHECKPOINT_REF = re.compile(
    r"checkpoint:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r":([0-9a-f]{64})"
)
_ATTACHMENT_REF = re.compile(
    r"attachment:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r":([0-9a-f]{64})"
)


def _clip(text: str | None, limit: int = TEXT_MAX) -> str | None:
    if text is None:
        return None
    return redact(text)[:limit]


def _reason(
    code: ResumabilityCause,
    subject: str,
    evidence_class: EvidenceClass,
    detail: str | None = None,
) -> Reason:
    return Reason(
        code=code,
        subject=_clip(subject) or "",
        evidence_class=evidence_class,
        detail=_clip(detail, DETAIL_MAX),
    )


async def _read_attachment(
    store: LocalArtifactStore,
    artifact: Artifact,
    *,
    read_bound: int,
) -> tuple[
    Availability, EvidenceClass, ResumabilityCause | None, int | None, str | None
]:
    """Re-hash an attachment through the existing store path. Never returns bytes."""
    path = _path(store, artifact)
    try:
        ap = anyio.Path(path)
        if not await ap.is_file():
            if await ap.exists():
                avail, ev, cause = classify_artifact(
                    recorded_size=artifact.size,
                    recorded_sha256=artifact.sha256,
                    observed_size=None,
                    observed_sha256=None,
                    error="is_directory",
                )
                return avail, ev, cause, None, None
            avail, ev, cause = classify_artifact(
                recorded_size=artifact.size,
                recorded_sha256=artifact.sha256,
                observed_size=None,
                observed_sha256=None,
                error="missing",
            )
            return avail, ev, cause, None, None
        if artifact.size > read_bound:
            avail, ev, cause = classify_artifact(
                recorded_size=artifact.size,
                recorded_sha256=artifact.sha256,
                observed_size=None,
                observed_sha256=None,
                error="too_large",
            )
            return avail, ev, cause, None, None
        # Read at most recorded size + 1 byte to detect overflow; never past the bound.
        budget = min(artifact.size, read_bound) + 1
        digest, size = hashlib.sha256(), 0
        async with await anyio.open_file(path, "rb") as f:
            while size < budget and (
                chunk := await f.read(min(1 << 20, budget - size))
            ):
                digest.update(chunk)
                size += len(chunk)
        sha = digest.hexdigest() if size <= artifact.size else None
        avail, ev, cause = classify_artifact(
            recorded_size=artifact.size,
            recorded_sha256=artifact.sha256,
            observed_size=size,
            observed_sha256=sha if size == artifact.size else None,
        )
        return avail, ev, cause, size, sha if size == artifact.size else None
    except PermissionError:
        avail, ev, cause = classify_artifact(
            recorded_size=artifact.size,
            recorded_sha256=artifact.sha256,
            observed_size=None,
            observed_sha256=None,
            error="permission",
        )
        return avail, ev, cause, None, None
    except FileNotFoundError:
        avail, ev, cause = classify_artifact(
            recorded_size=artifact.size,
            recorded_sha256=artifact.sha256,
            observed_size=None,
            observed_sha256=None,
            error="missing",
        )
        return avail, ev, cause, None, None
    except IsADirectoryError:
        avail, ev, cause = classify_artifact(
            recorded_size=artifact.size,
            recorded_sha256=artifact.sha256,
            observed_size=None,
            observed_sha256=None,
            error="is_directory",
        )
        return avail, ev, cause, None, None
    except OSError:
        avail, ev, cause = classify_artifact(
            recorded_size=artifact.size,
            recorded_sha256=artifact.sha256,
            observed_size=None,
            observed_sha256=None,
            error="os_error",
        )
        return avail, ev, cause, None, None
    except Exception:
        avail, ev, cause = classify_artifact(
            recorded_size=artifact.size,
            recorded_sha256=artifact.sha256,
            observed_size=None,
            observed_sha256=None,
            error="other",
        )
        return avail, ev, cause, None, None


def _published_artifact(cont: EffectiveContinuation) -> bool:
    return any(
        a.provenance is not ContinuationProvenance.WORKER
        and a.state is not ArtifactState.UNKNOWN
        and (a.head_sha is not None or a.kind.value in ("pr", "branch", "commit"))
        for a in cont.artifacts
    )


async def assess(
    conn: aiosqlite.Connection,
    task: Task,
    *,
    now: datetime,
    expected_attempt: int | None,
    expected_version: str | None,
    store: LocalArtifactStore,
    read_bound: int = DEFAULT_READ_BOUND,
) -> ResumabilityReport:
    """Read-only assessment. Callers hold a read connection; hashing uses the store
    only and never writes. One bad artifact becomes a reason, never a raise."""
    causes: list[ResumabilityCause] = []
    reasons: list[Reason] = []
    artifacts_out: list[ArtifactAssessment] = []
    unknowns: list[str] = []
    next_steps: list[NextStep] = []

    cont = task.continuation
    assert cont is not None

    # -- staleness ---------------------------------------------------------------
    if expected_attempt is not None and expected_attempt != task.attempt:
        causes.append(ResumabilityCause.ATTEMPT_MISMATCH)
        reasons.append(
            _reason(
                ResumabilityCause.ATTEMPT_MISMATCH,
                f"attempt:{task.attempt}",
                EvidenceClass.RECORDED_METADATA,
                f"expected attempt {expected_attempt}, task is on {task.attempt}",
            )
        )
    if expected_version is not None and expected_version != task.version:
        causes.append(ResumabilityCause.STALE_PREFLIGHT)
        reasons.append(
            _reason(
                ResumabilityCause.STALE_PREFLIGHT,
                f"version:{task.version}",
                EvidenceClass.RECORDED_METADATA,
                "expected_version no longer matches the task version",
            )
        )

    if task.state in TERMINAL:
        causes.append(ResumabilityCause.TASK_TERMINAL)
        reasons.append(
            _reason(
                ResumabilityCause.TASK_TERMINAL,
                f"state:{task.state.value}",
                EvidenceClass.RECORDED_METADATA,
                "task is terminal; a new attempt will not start from this state",
            )
        )

    # -- lease -------------------------------------------------------------------
    live = (
        task.assignee_agent_id is not None
        and task.lease_expires_at is not None
        and task.lease_expires_at > now
        and task.state.value == "claimed"
    )
    if (
        task.assignee_agent_id is not None
        and task.lease_expires_at is not None
        and task.lease_expires_at <= now
    ):
        causes.append(ResumabilityCause.LEASE_EXPIRED)
        reasons.append(
            _reason(
                ResumabilityCause.LEASE_EXPIRED,
                f"agent:{task.assignee_agent_id}",
                EvidenceClass.RECORDED_METADATA,
                "recorded lease expiry is in the past",
            )
        )
    elif live:
        causes.append(ResumabilityCause.LEASE_HELD_ELSEWHERE)
        reasons.append(
            _reason(
                ResumabilityCause.LEASE_HELD_ELSEWHERE,
                f"agent:{task.assignee_agent_id}",
                EvidenceClass.RECORDED_METADATA,
                "a live lease is held; release before starting elsewhere",
            )
        )
    lease = LeaseAssessment(
        assignee_agent_id=task.assignee_agent_id,
        expires_at=task.lease_expires_at,
        live=live,
    )

    # -- continuation artifacts (remote: never current-read) ---------------------
    if not cont.artifacts and cont.wip is None:
        causes.append(ResumabilityCause.NO_ARTIFACTS)
        unknowns.append("no_artifacts")
    for a in cont.artifacts[:MAX_ARTIFACTS]:
        if a.state is ArtifactState.UNKNOWN:
            causes.append(ResumabilityCause.ARTIFACT_STATE_UNOBSERVED)
            reasons.append(
                _reason(
                    ResumabilityCause.ARTIFACT_STATE_UNOBSERVED,
                    f"{a.kind.value}:{a.ref}",
                    EvidenceClass.RECORDED_METADATA,
                    _clip(a.note),
                )
            )
        artifacts_out.append(
            ArtifactAssessment(
                kind=a.kind.value,
                ref=_clip(a.ref) or a.ref[:TEXT_MAX],
                state=a.state.value,
                provenance=a.provenance.value,
                availability=Availability.UNKNOWN,
                evidence_class=EvidenceClass.RECORDED_METADATA,
                note=_clip(a.note),
            )
        )

    # -- inputs (shared attachments): receipts + current-read --------------------
    listed = await inputs_mod.list_inputs(conn, task.id, manager=True)
    caps = task.required_capabilities
    has_cap = INPUTS_CAPABILITY in caps
    if listed.inputs and not has_cap:
        capability: Literal[
            "required", "capability_not_required", "capability_unknown"
        ] = "capability_not_required"
        consumer = ConsumerPath.NONE
        causes.append(ResumabilityCause.INPUT_CAPABILITY_MISSING)
        reasons.append(
            _reason(
                ResumabilityCause.INPUT_CAPABILITY_MISSING,
                "capability:task-inputs",
                EvidenceClass.RECORDED_METADATA,
                "shared inputs exist but task-inputs is not in required_capabilities",
            )
        )
        next_steps.append(
            NextStep(
                code="require_task_inputs",
                detail="add task-inputs to required_capabilities before relying on inputs",
            )
        )
    elif has_cap:
        capability = "required"
        consumer = ConsumerPath.TASK_INPUTS_MATERIALIZER
    else:
        capability = "capability_unknown"
        consumer = ConsumerPath.NONE

    inputs_out: list[InputAssessment] = []
    required_inputs_ok = True
    required_unobserved = False
    by_id: dict[str, Artifact] = {}

    for item in listed.inputs[:MAX_INPUTS]:
        try:
            art = await get_artifact(conn, item.id)
        except Exception:
            required_inputs_ok = False
            causes.append(ResumabilityCause.ARTIFACT_UNKNOWN)
            reasons.append(
                _reason(
                    ResumabilityCause.ARTIFACT_UNKNOWN,
                    f"attachment:{item.id}",
                    EvidenceClass.UNKNOWN,
                    "artifact row could not be loaded",
                )
            )
            continue
        if art.source is not ArtifactSource.ATTACHMENT:
            continue
        by_id[art.id] = art
        avail, ev, cause, obs_size, obs_sha = await _read_attachment(
            store, art, read_bound=read_bound
        )
        if cause is not None:
            causes.append(cause)
            reasons.append(
                _reason(
                    cause,
                    f"attachment:{art.id}",
                    ev,
                    f"name={art.name}; recorded_size={art.size}",
                )
            )
        if avail is not Availability.AVAILABLE:
            required_inputs_ok = False
            if avail is Availability.UNKNOWN:
                required_unobserved = True
        if ev is EvidenceClass.UNKNOWN:
            required_unobserved = True
        receipts = [
            InputReceiptView(
                attempt=r.attempt,
                state=r.state.value,
                reason=r.reason.value if r.reason else None,
            )
            for r in item.receipts[:20]
        ]
        inputs_out.append(
            InputAssessment(
                artifact_id=art.id,
                name=art.name,
                recorded_size=art.size,
                recorded_sha256=art.sha256,
                availability=avail,
                evidence_class=ev,
                receipts=receipts,
                consumer_path=consumer,
                capability=capability,
            )
        )
        if len(artifacts_out) < MAX_ARTIFACTS:
            artifacts_out.append(
                ArtifactAssessment(
                    kind="attachment",
                    ref=art.name,
                    artifact_id=art.id,
                    state="attached",
                    provenance="orchestrator",
                    availability=avail,
                    evidence_class=ev,
                    recorded_size=art.size,
                    recorded_sha256=art.sha256,
                    observed_size=obs_size,
                    observed_sha256=obs_sha,
                )
            )

    # -- WIP ---------------------------------------------------------------------
    wip_model = cont.wip
    if wip_model is None:
        if _published_artifact(cont):
            wip = WipAssessment(
                kind=WipKind.PUBLISHED,
                evidence_class=EvidenceClass.RECORDED_METADATA,
                transfer="published_ref",
            )
        else:
            causes.append(ResumabilityCause.WIP_UNRECORDED)
            unknowns.append("wip_unrecorded")
            wip = WipAssessment(
                kind=WipKind.ABSENT,
                evidence_class=EvidenceClass.UNOBSERVED,
            )
    else:
        ref = wip_model.ref
        summary = _clip(wip_model.summary)
        if ref and (m := _CHECKPOINT_REF.fullmatch(ref)):
            causes.append(ResumabilityCause.CHECKPOINT_STORE_NOT_VISIBLE)
            reasons.append(
                _reason(
                    ResumabilityCause.CHECKPOINT_STORE_NOT_VISIBLE,
                    f"checkpoint:{m.group(1)}",
                    EvidenceClass.UNOBSERVED,
                    "a checkpoint id is node-local and is not a transfer",
                )
            )
            next_steps.append(
                NextStep(
                    code="publish_or_attach",
                    detail="publish a branch/PR or attach bytes; a checkpoint is not transferable",
                )
            )
            wip = WipAssessment(
                kind=WipKind.UNKNOWN,
                ref=_clip(ref),
                summary=summary,
                availability=Availability.UNKNOWN,
                evidence_class=EvidenceClass.UNOBSERVED,
                transfer="not_a_transfer",
            )
            required_unobserved = True
        elif ref and (m := _ATTACHMENT_REF.fullmatch(ref)):
            aid, expect_sha = m.group(1), m.group(2)
            art = by_id.get(aid)
            if art is None:
                try:
                    art = await get_artifact(conn, aid)
                except Exception:
                    art = None
            if art is None or art.source is not ArtifactSource.ATTACHMENT:
                causes.append(ResumabilityCause.ARTIFACT_MISSING)
                required_inputs_ok = False
                reasons.append(
                    _reason(
                        ResumabilityCause.ARTIFACT_MISSING,
                        f"attachment:{aid}",
                        EvidenceClass.CURRENT_READ,
                        "WIP attachment reference has no matching attachment",
                    )
                )
                wip = WipAssessment(
                    kind=WipKind.UNKNOWN,
                    ref=_clip(ref),
                    summary=summary,
                    availability=Availability.MISSING,
                    evidence_class=EvidenceClass.CURRENT_READ,
                    transfer="not_a_transfer",
                )
            elif art.sha256 != expect_sha:
                causes.append(ResumabilityCause.ARTIFACT_CORRUPTED)
                required_inputs_ok = False
                reasons.append(
                    _reason(
                        ResumabilityCause.ARTIFACT_CORRUPTED,
                        f"attachment:{aid}",
                        EvidenceClass.RECORDED_METADATA,
                        "WIP ref sha256 does not match the recorded attachment",
                    )
                )
                wip = WipAssessment(
                    kind=WipKind.UNKNOWN,
                    ref=_clip(ref),
                    summary=summary,
                    availability=Availability.CORRUPTED,
                    evidence_class=EvidenceClass.RECORDED_METADATA,
                    transfer="not_a_transfer",
                )
            else:
                avail, ev, cause, _, _ = await _read_attachment(
                    store, art, read_bound=read_bound
                )
                if cause is not None:
                    causes.append(cause)
                    reasons.append(
                        _reason(cause, f"attachment:{aid}", ev, "WIP attachment ref")
                    )
                if avail is not Availability.AVAILABLE:
                    required_inputs_ok = False
                wip = WipAssessment(
                    kind=WipKind.PUBLISHED
                    if avail is Availability.AVAILABLE
                    else WipKind.UNKNOWN,
                    ref=_clip(ref),
                    summary=summary,
                    availability=avail,
                    evidence_class=ev,
                    transfer="attachment_verified"
                    if avail is Availability.AVAILABLE
                    else "not_a_transfer",
                )
        elif _published_artifact(cont):
            wip = WipAssessment(
                kind=WipKind.PUBLISHED,
                ref=_clip(ref),
                summary=summary,
                evidence_class=EvidenceClass.RECORDED_METADATA,
                transfer="published_ref",
            )
        else:
            causes.append(ResumabilityCause.WIP_LOCAL_ONLY)
            reasons.append(
                _reason(
                    ResumabilityCause.WIP_LOCAL_ONLY,
                    _clip(ref) or "wip",
                    EvidenceClass.RECORDED_METADATA,
                    "a workspace path or local sha is not a transfer to another attempt",
                )
            )
            next_steps.append(
                NextStep(
                    code="publish_wip",
                    detail="push a branch/PR or attach the bytes before resuming elsewhere",
                )
            )
            wip = WipAssessment(
                kind=WipKind.LOCAL_ONLY,
                ref=_clip(ref),
                summary=summary,
                evidence_class=EvidenceClass.RECORDED_METADATA,
                transfer="not_a_transfer",
            )
            required_unobserved = True

    # -- decisions / gaps --------------------------------------------------------
    decision_changes: list[DecisionChange] = []
    for d in cont.decisions[:MAX_DECISION_CHANGES]:
        if d.status == "unresolved":
            rel: Literal["at_or_after", "before", "unresolved"] = "unresolved"
            causes.append(ResumabilityCause.DECISION_UNRESOLVED)
        elif d.attempt is not None and d.attempt >= task.attempt:
            rel = "at_or_after"
        else:
            rel = "before"
        decision_changes.append(
            DecisionChange(
                decision_id=d.decision_id,
                status=d.status,
                question=_clip(d.question) or "",
                decided_at=d.decided_at,
                premise_outdated=d.premise_outdated,
                relative_to_attempt=rel,
            )
        )
    for gap in cont.gaps[:MAX_UNKNOWNS]:
        unknowns.append(gap[:TEXT_MAX])
        if gap == "acceptance_not_structured":
            causes.append(ResumabilityCause.ACCEPTANCE_NOT_STRUCTURED)
        elif gap.startswith("decision_unresolved:"):
            causes.append(ResumabilityCause.DECISION_UNRESOLVED)
        elif gap == "artifact_state_unobserved":
            causes.append(ResumabilityCause.ARTIFACT_STATE_UNOBSERVED)
        elif gap == "artifacts_unknown":
            causes.append(ResumabilityCause.NO_ARTIFACTS)

    # Deduplicate causes while preserving order.
    seen: set[ResumabilityCause] = set()
    uniq_causes: list[ResumabilityCause] = []
    for c in causes:
        if c not in seen:
            seen.add(c)
            uniq_causes.append(c)

    decision = derive_decision(
        uniq_causes,
        required_inputs_ok=required_inputs_ok,
        required_unobserved=required_unobserved,
    )
    if decision is ResumabilityDecision.RESUMABLE and not next_steps:
        next_steps.append(
            NextStep(
                code="proceed",
                detail="required inputs are current_read/available; preflight grants nothing",
            )
        )

    return ResumabilityReport(
        task_id=task.id,
        attempt=task.attempt,
        task_version=task.version,
        assessed_at=now,
        decision=decision,
        cause_codes=uniq_causes[:20],
        reasons=reasons[:MAX_REASONS],
        artifacts=artifacts_out[:MAX_ARTIFACTS],
        wip=wip,
        inputs=inputs_out[:MAX_INPUTS],
        lease=lease,
        decision_changes=decision_changes[:MAX_DECISION_CHANGES],
        unknowns=unknowns[:MAX_UNKNOWNS],
        next_steps=next_steps[:MAX_NEXT_STEPS],
    )
