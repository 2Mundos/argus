from typing import Annotated

from argus_core.decisions import (
    Ask,
    Decision,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)
from argus_core.tasks import TaskState
from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from argus_api import inbox as inbox_module
from argus_api.decisions import service, store, views
from argus_api.decisions.service import AnswerBatch, AnsweredDecision, AnswerOutcome
from argus_api.decisions.views import DecisionPage
from argus_api.deps import AgentDep, CallerDep, DbDep, ManagerDep
from argus_api.inbox import InboxPage
from argus_api.paging import DEFAULT_LIMIT, View

router = APIRouter(tags=["decisions"])


@router.get("/inbox")
async def inbox(
    db: DbDep,
    _: ManagerDep,
    project: str | None = None,
    state: Annotated[list[TaskState] | None, Query()] = None,
    decision_kind: Annotated[list[DecisionKind] | None, Query()] = None,
    view: View = "summary",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> InboxPage:
    """What waits on a manager, oldest first: blocked, in-review and
    awaiting-approval tasks with their open decision, plus open decisions about
    pools and knowledge. `state` keeps only task rows in those states."""
    async with db.snapshot() as snap:
        return await inbox_module.inbox(
            snap,
            project=project,
            states=state,
            decision_kinds=decision_kind,
            view=view,
            limit=limit,
            cursor=cursor,
        )


@router.get("/decisions")
async def list_decisions(
    db: DbDep,
    _: CallerDep,
    state: DecisionState | None = DecisionState.OPEN,
    subject_id: str | None = None,
    project: str | None = None,
    kind: Annotated[list[DecisionKind] | None, Query()] = None,
    view: View = "summary",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> DecisionPage:
    """Decisions oldest first, in pages (max 100); open ones by default."""
    async with db.snapshot() as snap:
        return await views.list_decisions(
            snap,
            state=state,
            subject_id=subject_id,
            project=project,
            kinds=kind,
            view=view,
            limit=limit,
            cursor=cursor,
        )


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str, db: DbDep, _: CallerDep) -> Decision:
    async with db.read() as conn:
        return await store.get(conn, decision_id)


@router.post("/decisions/{decision_id}/answer")
async def answer_decision(
    decision_id: str, body: DecisionAnswer, db: DbDep, caller: ManagerDep
) -> AnsweredDecision:
    """Answer an open decision. Approvals take `approve`/`reject`, reviews
    `accept`/`reject` (with `text` as notes), questions their options or text.
    With a `request_id`, a retry of the same answer by the same caller returns
    the stored decision with `replayed: true` instead of 409 decision_not_open."""
    async with db.write() as tx:
        return await service.answer(tx, decision_id, body, caller)


class AnswerBatchResult(BaseModel):
    results: list[AnswerOutcome]  # one per answer, in order


@router.post("/decisions/answers")
async def answer_decisions(
    body: AnswerBatch, db: DbDep, caller: ManagerDep
) -> AnswerBatchResult:
    """Answer up to 20 decisions, each with its own explicit answer and in its own
    transaction. Each item succeeds or fails on its own (`ok`, `error`)."""
    return AnswerBatchResult(
        results=await service.answer_many(db, body.answers, caller)
    )


@router.post("/tasks/{task_id}/ask", status_code=status.HTTP_201_CREATED)
async def ask(task_id: str, body: Ask, db: DbDep, agent_id: AgentDep) -> Decision:
    """Ask a question about the task you hold. The task is blocked until it is
    answered; the answer is added to the task spec under "Clarifications" and
    the task returns to ready."""
    async with db.write() as tx:
        return await service.ask(tx, agent_id, task_id, body)
