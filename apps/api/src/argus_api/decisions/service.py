"""Asking and answering decisions; answers drive task transitions (3.10)."""

import aiosqlite
from argus_core.auth import Scope
from argus_core.decisions import (
    Ask,
    Audience,
    Decision,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)

from argus_api.auth import Identity
from argus_api.db import Tx, ts
from argus_api.decisions import store
from argus_api.errors import ConflictError, ForbiddenError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import service as tasks


async def list_decisions(
    conn: aiosqlite.Connection,
    *,
    state: DecisionState | None = DecisionState.OPEN,
    subject_id: str | None = None,
) -> list[Decision]:
    sql, params = "SELECT * FROM decisions WHERE 1 = 1", []
    if state is not None:
        sql += " AND state = ?"
        params.append(state)
    if subject_id is not None:
        sql += " AND subject_id = ?"
        params.append(subject_id)
    async with conn.execute(sql + " ORDER BY created_at, id", params) as cur:
        return [store.to_decision(r) for r in await cur.fetchall()]


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


async def answer(
    tx: Tx, decision_id: str, body: DecisionAnswer, caller: Identity
) -> Decision:
    decision = await store.get(tx.conn, decision_id)
    if decision.state is not DecisionState.OPEN:
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
        " decided_by = ?, decided_at = ? WHERE id = ?",
        (body.choice, body.text or None, caller.actor, ts(tx.now), decision_id),
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
    return await store.get(tx.conn, decision_id)


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
            await tasks.unblock(tx, task_id, caller.actor)
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
