"""Asking and answering decisions; answers drive task transitions (3.10)."""

from argus_core.auth import Scope
from argus_core.decisions import (
    Ask,
    Audience,
    Decision,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)
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
from argus_api.tasks import service as tasks


async def ask(tx: Tx, agent_id: str, task_id: str, body: Ask) -> Decision:
    """A worker asks a question about the task it holds. The task is blocked
    (its lease released) until the question is answered."""
    decision_id = await store.open_decision(
        tx,
        subject_type="task",
        subject_id=task_id,
        kind=DecisionKind.QUESTION,
        question=body.question,
        options=body.options,
        audience=body.audience,
        requested_by=f"agent:{agent_id}",
    )
    await tasks.block(
        tx, agent_id, task_id, body.attempt, f"waiting for decision {decision_id}"
    )
    return await store.get(tx.conn, decision_id)


class AnsweredDecision(Decision):
    replayed: bool = False  # a retry of the stored answer; nothing changed


async def answer(
    tx: Tx, decision_id: str, body: DecisionAnswer, caller: Identity
) -> AnsweredDecision:
    decision = await store.get(tx.conn, decision_id)
    if decision.state is not DecisionState.OPEN:
        if body.request_id is not None and _same_answer(decision, body, caller):
            return AnsweredDecision(**decision.model_dump(), replayed=True)
        raise ConflictError(
            f"decision {decision_id} is {decision.state}", code="decision_not_open"
        )
    _check_audience(decision.audience, caller.scope)
    if decision.options is not None and body.choice not in decision.options:
        raise ValidationFailedError(
            f"choice must be one of: {', '.join(decision.options)}",
            code="invalid_choice",
        )
    if decision.options is None and not body.text:
        raise ValidationFailedError("this question needs a text answer")

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
    await _apply_answer(tx, decision, body, caller)
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
    decision_id: str
    ok: bool
    decision: AnsweredDecision | None = None
    error: ItemError | None = None


async def answer_many(
    db: Database, items: list[AnswerItem], caller: Identity
) -> list[AnswerOutcome]:
    """Answer each item in its own transaction, in order; one failure never
    rolls back or skips the others."""
    outcomes = []
    for item in items:
        body = DecisionAnswer(**item.model_dump(exclude={"decision_id"}))
        try:
            async with db.write() as tx:
                decision = await answer(tx, item.decision_id, body, caller)
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
                AnswerOutcome(decision_id=item.decision_id, ok=True, decision=decision)
            )
    return outcomes


def _same_answer(decision: Decision, body: DecisionAnswer, caller: Identity) -> bool:
    return (
        decision.state is DecisionState.ANSWERED
        and decision.request_id == body.request_id
        and decision.decided_by == caller.actor
        and decision.answer_choice == body.choice
        and (decision.answer_text or "") == body.text
    )


async def _apply_answer(
    tx: Tx, decision: Decision, body: DecisionAnswer, caller: Identity
) -> None:
    task_id = decision.subject_id
    match decision.kind:
        case DecisionKind.QUESTION:
            answer_text = " — ".join(x for x in (body.choice, body.text) if x)
            await tasks.add_clarification(
                tx, task_id, decision.question, answer_text, caller.actor
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
        case DecisionKind.KNOWLEDGE:
            await knowledge.resolve_proposal(
                tx,
                decision.subject_id,
                accept=body.choice == "accept",
                actor=caller.actor,
            )
        case DecisionKind.POLICY if decision.subject_type == "pool":
            await fleet.apply_scale_decision(
                tx,
                decision.subject_id,
                decision.payload or {},
                approve=body.choice == "approve",
                actor=caller.actor,
            )
        case DecisionKind.APPROVAL | DecisionKind.POLICY:
            if body.choice == "approve":
                await tasks.approve(tx, task_id, caller.actor)
            else:
                await tasks.cancel(tx, task_id, caller.actor)
        case DecisionKind.REVIEW:
            await tasks.review(
                tx,
                task_id,
                caller.actor,
                caller.scope,
                accept=body.choice == "accept",
                notes=body.text,
            )


def _check_audience(audience: Audience, scope: Scope) -> None:
    allowed = {
        Audience.HUMAN: {Scope.ADMIN},
        Audience.ORCHESTRATOR: {Scope.ORCHESTRATOR, Scope.ADMIN},
        Audience.ANY: {Scope.ORCHESTRATOR, Scope.ADMIN},
    }[audience]
    if scope not in allowed:
        raise ForbiddenError(f"this decision is for: {audience}")
