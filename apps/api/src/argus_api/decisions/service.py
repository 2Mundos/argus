"""Asking and answering decisions; answers drive task transitions (3.10)."""

from datetime import datetime
from enum import StrEnum

from argus_core.auth import Scope
from argus_core.decisions import (
    Ask,
    Audience,
    Decision,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)
from argus_core.tasks import FORGE_DELIVERABLES, TaskState
from pydantic import BaseModel, Field

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
from argus_api.routines import service as routines
from argus_api.tasks import deliverable, delivery
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
    if choice != "accept":
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
        if (
            task.deliverable not in FORGE_DELIVERABLES
            or task.state is not TaskState.IN_REVIEW
        ):
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
        if body.request_id is not None and same_answer(decision, body, caller):
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
    TASK_APPROVE = "task_approve"
    TASK_CANCEL = "task_cancel"
    REVIEW_ACCEPT = "review_accept"
    REVIEW_REJECT = "review_reject"


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
        case DecisionKind.APPROVAL | DecisionKind.POLICY:
            return Effect.TASK_APPROVE if choice == "approve" else Effect.TASK_CANCEL
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
        case Effect.ROUTINE_STAY_OFF:
            pass
        case Effect.TASK_APPROVE:
            await tasks.approve(tx, task_id, caller.actor)
        case Effect.TASK_CANCEL:
            await tasks.cancel(tx, task_id, caller.actor)
        case Effect.REVIEW_ACCEPT | Effect.REVIEW_REJECT as effect:
            await tasks.review(
                tx,
                task_id,
                caller.actor,
                caller.scope,
                accept=effect is Effect.REVIEW_ACCEPT,
                notes=body.text,
                forge=forge,
            )


def check_answer(decision: Decision, body: DecisionAnswer) -> None:
    """The choice and text rules of the answer path (raises 422)."""
    if decision.options is not None and body.choice not in decision.options:
        raise ValidationFailedError(
            f"choice must be one of: {', '.join(decision.options)}",
            code="invalid_choice",
        )
    if decision.options is None and not body.text:
        raise ValidationFailedError("this question needs a text answer")


def check_audience(audience: Audience, scope: Scope) -> None:
    allowed = {
        Audience.HUMAN: {Scope.ADMIN},
        Audience.ORCHESTRATOR: {Scope.ORCHESTRATOR, Scope.ADMIN},
        Audience.ANY: {Scope.ORCHESTRATOR, Scope.ADMIN},
    }[audience]
    if scope not in allowed:
        raise ForbiddenError(f"this decision is for: {audience}")
