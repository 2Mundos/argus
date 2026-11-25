"""Asking and answering decisions; answers drive task transitions (3.10)."""

from datetime import datetime
from enum import StrEnum

from argus_core.auth import Scope
from argus_core.decisions import (
    ACCEPT_WITH_DEVIATION,
    APPROVE_WITHOUT_REVIEW,
    FINDINGS_REFUTED_GATE,
    GATE_DISABLED_GATE,
    NO_REVIEWER_GATE,
    RETRY_REVIEWER,
    REVIEW_FAILED_GATE,
    REVIEW_OPTIONS,
    REVIEWER_FAILED_GATE,
    Ask,
    Audience,
    Decision,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)
from argus_core.tasks import FORGE_DELIVERABLES, TaskState
from pydantic import BaseModel, Field

from argus_api.accounts import service as accounts
from argus_api.auth import Identity
from argus_api.db import Database, Tx, ts
from argus_api.decisions import store
from argus_api.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.fleet import service as fleet
from argus_api.knowledge import service as knowledge
from argus_api.routines import findings
from argus_api.routines import service as routines
from argus_api.tasks import deliverable, delivery, independent_review
from argus_api.tasks import service as tasks


async def ask(tx: Tx, agent_id: str, task_id: str, body: Ask) -> Decision:
    """A worker asks a question about the task it holds. The task is blocked
    (its lease released) until the question is answered."""
    await tasks.check_lease(tx, task_id, body.attempt, agent_id)
    assistance = body.category == "assistance"
    if not assistance and (
        known := await tasks.answered_equivalent(tx, task_id, body.question)
    ):
        # Already decided: no new gate and no block. The worker is told the answer.
        raise ConflictError(
            f"already answered ({known.decided_at:%Y-%m-%d}): {known.answer}",
            code="already_answered",
        )
    decision_id = await store.open_decision(
        tx,
        subject_type="task",
        subject_id=task_id,
        kind=DecisionKind.QUESTION,
        question=body.question,
        options=body.options,
        audience=Audience.ORCHESTRATOR if assistance else body.audience,
        requested_by=f"agent:{agent_id}",
        payload={"category": body.category, "resource": body.resource}
        if assistance
        else None,
    )
    await tasks.block(
        tx, agent_id, task_id, body.attempt, f"waiting for decision {decision_id}"
    )
    return await store.get(tx.conn, decision_id)


class AnsweredDecision(Decision):
    replayed: bool = False  # a retry of the stored answer; nothing changed


async def forge_evidence(
    db: Database, checker: delivery.PrChecker, decision_id: str, choice: str | None
) -> deliverable.ForgeRead | None:
    """For accepting a review of a task that must be green or merged: what the forge
    shows, read here, before and outside the write transaction (task deliverable
    spec 3.5). None when no forge read is needed or the decision cannot be read; the
    answer reports the latter itself."""
    if choice not in (
        "accept",
        ACCEPT_WITH_DEVIATION,
        APPROVE_WITHOUT_REVIEW,
        RETRY_REVIEWER,
    ):
        return None
    async with db.read() as conn:
        try:
            decision = await store.get(conn, decision_id)
            if (
                decision.kind is not DecisionKind.REVIEW
                or decision.state is not DecisionState.OPEN
            ):
                return None
            task = await tasks.get_task(conn, decision.subject_id)
        except DomainError:
            return None
        if choice == RETRY_REVIEWER:
            # A retry may need the head the forge reports when no reviewer had one.
            if (
                not review_failed_gate(decision)
                or task.state is not TaskState.IN_REVIEW
            ):
                return None
            return deliverable.ForgeRead(
                await delivery.delivery_of(conn, checker, task)
            )
        if task.state is not TaskState.IN_REVIEW:
            return None
        from argus_api.settings import service as settings_svc

        review_setting = await settings_svc.get_review(
            conn, independent_review.model_policy_path, task.project
        )
        if task.deliverable not in FORGE_DELIVERABLES and review_setting.mode == "off":
            return None
        return deliverable.ForgeRead(await delivery.delivery_of(conn, checker, task))


async def answer(
    tx: Tx,
    decision_id: str,
    body: DecisionAnswer,
    caller: Identity,
    forge: deliverable.ForgeRead | None = None,
) -> AnsweredDecision:
    decision = await store.get(tx.conn, decision_id)
    if decision.state is not DecisionState.OPEN:
        if (
            body.request_id is not None
            and same_answer(decision, body, caller)
            and bool(body.promote) == await was_promoted(tx, decision)
        ):
            return AnsweredDecision(**decision.model_dump(), replayed=True)
        raise ConflictError(
            f"decision {decision_id} is {decision.state}", code="decision_not_open"
        )
    check_audience(decision.audience, caller.scope)
    check_answer(decision, body)

    # Close the decision first: the transition below would otherwise cancel it
    # as moot (store.WAITS_IN).
    await tx.conn.execute(
        "UPDATE decisions SET state = 'answered', answer_choice = ?, answer_text = ?,"
        " decided_by = ?, decided_at = ?, request_id = ? WHERE id = ?",
        (
            body.choice,
            body.text or None,
            caller.actor,
            ts(tx.now),
            body.request_id,
            decision_id,
        ),
    )
    await record(
        tx,
        caller.actor,
        "decision",
        decision_id,
        "decision.answered",
        {"choice": body.choice},
    )
    await _apply_answer(tx, decision, body, caller, forge)
    return AnsweredDecision(**(await store.get(tx.conn, decision_id)).model_dump())


class AnswerItem(DecisionAnswer):
    decision_id: str


MAX_BATCH = 20


class AnswerBatch(BaseModel):
    answers: list[AnswerItem] = Field(min_length=1, max_length=MAX_BATCH)


class ItemError(BaseModel):
    status: int
    code: str
    message: str
    details: list[str] = Field(default_factory=list)


class AnswerOutcome(BaseModel):
    """One item's result: what was decided, not the whole decision (its question
    is already known to the caller); get_decision reads it in full."""

    decision_id: str
    ok: bool
    replayed: bool = False
    subject_type: str | None = None
    subject_id: str | None = None
    answer_choice: str | None = None
    decided_at: datetime | None = None
    error: ItemError | None = None


async def answer_many(
    db: Database,
    items: list[AnswerItem],
    caller: Identity,
    checker: delivery.PrChecker,
) -> list[AnswerOutcome]:
    """Answer each item in its own transaction, in order; one failure never
    rolls back or skips the others."""
    outcomes = []
    for item in items:
        body = DecisionAnswer(**item.model_dump(exclude={"decision_id"}))
        try:
            forge = await forge_evidence(db, checker, item.decision_id, body.choice)
            async with db.write() as tx:
                decision = await answer(tx, item.decision_id, body, caller, forge)
        except DomainError as exc:
            error = ItemError(
                status=exc.status,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            )
            outcomes.append(
                AnswerOutcome(decision_id=item.decision_id, ok=False, error=error)
            )
        else:
            outcomes.append(
                AnswerOutcome(
                    decision_id=item.decision_id,
                    ok=True,
                    replayed=decision.replayed,
                    subject_type=decision.subject_type,
                    subject_id=decision.subject_id,
                    answer_choice=decision.answer_choice,
                    decided_at=decision.decided_at,
                )
            )
    return outcomes


async def was_promoted(tx: Tx, decision: Decision) -> bool:
    """Whether answering this decision promoted its task to a PR deliverable. The
    audit event is the record: the answer row does not carry `promote`, so a replay
    that disagrees about it is a different answer, not a retry."""
    async with tx.conn.execute(
        "SELECT 1 FROM events WHERE kind = 'task.promoted'"
        " AND json_extract(data, '$.decision_id') = ?",
        (decision.id,),
    ) as cur:
        return await cur.fetchone() is not None


def same_answer(decision: Decision, body: DecisionAnswer, caller: Identity) -> bool:
    return (
        decision.state is DecisionState.ANSWERED
        and decision.request_id == body.request_id
        and decision.decided_by == caller.actor
        and decision.answer_choice == body.choice
        and (decision.answer_text or "") == body.text
    )


class Effect(StrEnum):
    """What answering does to the decision's subject. The answer path and the
    preview both dispatch on this, so they cannot disagree."""

    CLARIFY = "clarify"  # question: add the answer to the spec, unblock the task
    KNOWLEDGE_ACCEPT = "knowledge_accept"
    KNOWLEDGE_REJECT = "knowledge_reject"
    POOL_SCALE_APPROVE = "pool_scale_approve"
    POOL_SCALE_REJECT = "pool_scale_reject"
    ROUTINE_REENABLE = "routine_reenable"
    ROUTINE_STAY_OFF = "routine_stay_off"
    ACCOUNT_MARK_AVAILABLE = "account_mark_available"
    ACCOUNT_DISMISS = "account_dismiss"
    TASK_APPROVE = "task_approve"
    TASK_CANCEL = "task_cancel"
    REVIEW_ACCEPT = "review_accept"
    REVIEW_ACCEPT_DEVIATION = "review_accept_deviation"
    REVIEW_REJECT = "review_reject"
    REVIEWER_RETRY = "reviewer_retry"
    REVIEWER_WAIVE = "reviewer_waive"


def review_failed_gate(decision: Decision) -> bool:
    """The decision opened when no independent reviewer delivered a verdict."""
    return (
        decision.kind is DecisionKind.REVIEW
        and (decision.payload or {}).get("gate") == REVIEW_FAILED_GATE
    )


def effect_of(decision: Decision, choice: str | None) -> Effect:
    match decision.kind:
        case DecisionKind.QUESTION:
            return Effect.CLARIFY
        case DecisionKind.KNOWLEDGE:
            accept = choice == "accept"
            return Effect.KNOWLEDGE_ACCEPT if accept else Effect.KNOWLEDGE_REJECT
        case DecisionKind.POLICY if decision.subject_type == "pool":
            approve = choice == "approve"
            return Effect.POOL_SCALE_APPROVE if approve else Effect.POOL_SCALE_REJECT
        case DecisionKind.POLICY if decision.subject_type == "routine":
            approve = choice == "approve"
            return Effect.ROUTINE_REENABLE if approve else Effect.ROUTINE_STAY_OFF
        case DecisionKind.POLICY if decision.subject_type == "account":
            approve = choice == "approve"
            return Effect.ACCOUNT_MARK_AVAILABLE if approve else Effect.ACCOUNT_DISMISS
        case DecisionKind.APPROVAL | DecisionKind.POLICY:
            return Effect.TASK_APPROVE if choice == "approve" else Effect.TASK_CANCEL
        case DecisionKind.REVIEW if review_failed_gate(decision):
            match choice:
                case "retry_reviewer":
                    return Effect.REVIEWER_RETRY
                case "approve_without_review":
                    return Effect.REVIEWER_WAIVE
                case _:
                    return Effect.TASK_CANCEL
        case DecisionKind.REVIEW if choice == ACCEPT_WITH_DEVIATION:
            return Effect.REVIEW_ACCEPT_DEVIATION
        case DecisionKind.REVIEW:
            return Effect.REVIEW_ACCEPT if choice == "accept" else Effect.REVIEW_REJECT


async def _apply_answer(
    tx: Tx,
    decision: Decision,
    body: DecisionAnswer,
    caller: Identity,
    forge: deliverable.ForgeRead | None = None,
) -> None:
    task_id = decision.subject_id
    match effect_of(decision, body.choice):
        case Effect.CLARIFY:
            answer_text = " — ".join(x for x in (body.choice, body.text) if x)
            await tasks.add_clarification(
                tx,
                task_id,
                decision.question,
                answer_text,
                caller.actor,
                decision_id=decision.id,
                choice=body.choice,
                text=body.text,
                options=decision.options,
                category=(decision.payload or {}).get("category", "decision"),
            )
            if body.save_to_knowledge is not None:
                task = await tasks.get_task(tx.conn, task_id)
                await knowledge.save_answer(
                    tx,
                    body.save_to_knowledge,
                    question=decision.question,
                    answer=answer_text,
                    decision_id=decision.id,
                    task_id=task_id,
                    task_project=task.project,
                    actor=caller.actor,
                )
            await tasks.unblock(tx, task_id, caller.actor)
        case Effect.KNOWLEDGE_ACCEPT | Effect.KNOWLEDGE_REJECT as effect:
            await knowledge.resolve_proposal(
                tx,
                decision.subject_id,
                accept=effect is Effect.KNOWLEDGE_ACCEPT,
                actor=caller.actor,
            )
        case Effect.POOL_SCALE_APPROVE | Effect.POOL_SCALE_REJECT as effect:
            await fleet.apply_scale_decision(
                tx,
                decision.subject_id,
                decision.payload or {},
                approve=effect is Effect.POOL_SCALE_APPROVE,
                actor=caller.actor,
            )
        case Effect.ROUTINE_REENABLE:
            await routines.reenable(tx, decision.subject_id, caller.actor)
        case Effect.ROUTINE_STAY_OFF | Effect.ACCOUNT_DISMISS:
            pass
        case Effect.ACCOUNT_MARK_AVAILABLE:
            await accounts.mark_available(tx, decision.subject_id, caller.actor)
        case Effect.TASK_APPROVE:
            if body.promote:
                await findings.promote_to_pr(
                    tx, task_id, decision.id, caller.actor, caller.scope
                )
            await tasks.approve(tx, task_id, caller.actor)
        case Effect.TASK_CANCEL:
            await tasks.cancel(tx, task_id, caller.actor)
        case Effect.REVIEWER_RETRY:
            await independent_review.retry_reviewer(
                tx, task_id, caller.actor, forge=forge
            )
        case Effect.REVIEWER_WAIVE:
            await independent_review.approve_without_review(
                tx,
                task_id,
                caller.actor,
                caller.scope,
                decision_id=decision.id,
                notes=body.text,
                forge=forge,
            )
        case (
            Effect.REVIEW_ACCEPT | Effect.REVIEW_ACCEPT_DEVIATION | Effect.REVIEW_REJECT
        ) as effect:
            manual_fallback = (decision.payload or {}).get("gate") in (
                NO_REVIEWER_GATE,
                REVIEWER_FAILED_GATE,
                GATE_DISABLED_GATE,
            )
            payload = decision.payload or {}
            refuted_child = (
                payload.get("review_child_id")
                if payload.get("gate") == FINDINGS_REFUTED_GATE
                else None
            )
            if refuted_child and effect is Effect.REVIEW_REJECT:
                await independent_review.release_refuted(
                    tx, task_id, refuted_child, caller.actor
                )
                return
            if refuted_child and effect is Effect.REVIEW_ACCEPT_DEVIATION:
                manual_fallback = True
                await independent_review.waive_exhausted(
                    tx,
                    task_id,
                    caller.actor,
                    decision_id=decision.id,
                    notes=body.text,
                    child_id=refuted_child,
                    reason=FINDINGS_REFUTED_GATE,
                )
            elif effect is Effect.REVIEW_ACCEPT_DEVIATION:
                unresolved = await independent_review.exhausted_child_of(
                    tx.conn, decision
                )
                if unresolved is not None:
                    manual_fallback = True
                    await independent_review.waive_exhausted(
                        tx,
                        task_id,
                        caller.actor,
                        decision_id=decision.id,
                        notes=body.text,
                        child_id=unresolved,
                    )
            await tasks.review(
                tx,
                task_id,
                caller.actor,
                caller.scope,
                accept=effect is not Effect.REVIEW_REJECT,
                notes=body.text,
                forge=forge,
                deviation=effect is Effect.REVIEW_ACCEPT_DEVIATION,
                waive_independent_review=manual_fallback,
            )
            await independent_review.clear_gate_block(tx, task_id)


def check_answer(decision: Decision, body: DecisionAnswer) -> None:
    """The choice and text rules of the answer path (raises 422)."""
    options = decision.options
    if (
        decision.kind is DecisionKind.REVIEW
        and options is not None
        and not review_failed_gate(decision)
    ):
        # Review decisions opened before the choice existed take it too (3.10).
        options = [*options, *(o for o in REVIEW_OPTIONS if o not in options)]
    if options is not None and body.choice not in options:
        raise ValidationFailedError(
            f"choice must be one of: {', '.join(options)}",
            code="invalid_choice",
        )
    if options is None and not body.text:
        raise ValidationFailedError("this question needs a text answer")
    if body.promote and not (
        decision.kind is DecisionKind.APPROVAL and body.choice == "approve"
    ):
        raise ValidationFailedError(
            "promote applies only to approving a task approval decision",
            code="promote_not_approval",
        )
    deviation = (
        decision.kind is DecisionKind.REVIEW and body.choice == ACCEPT_WITH_DEVIATION
    )
    if deviation and not body.text.strip():
        raise ValidationFailedError(
            "accept_with_deviation needs text: which criteria are unmet, why the"
            " result is accepted anyway, and who authorized it",
            code="deviation_reason_required",
        )


def check_audience(audience: Audience, scope: Scope) -> None:
    allowed = {
        Audience.HUMAN: {Scope.ADMIN},
        Audience.ORCHESTRATOR: {Scope.ORCHESTRATOR, Scope.ADMIN},
        Audience.ANY: {Scope.ORCHESTRATOR, Scope.ADMIN},
    }[audience]
    if scope not in allowed:
        raise ForbiddenError(f"this decision is for: {audience}")
