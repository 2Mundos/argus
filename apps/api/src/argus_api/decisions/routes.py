from typing import Annotated

from argus_core.decisions import (
    Ask,
    Decision,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)
from argus_core.tasks import TaskState
from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel

from argus_api import inbox as inbox_module
from argus_api import inbox_actions
from argus_api.access import require_decision_readable, worker_decision_filter
from argus_api.decisions import preview as preview_module
from argus_api.decisions import service, store, views
from argus_api.decisions.service import AnswerBatch, AnsweredDecision, AnswerOutcome
from argus_api.decisions.views import DecisionPage
from argus_api.deps import AgentDep, CallerDep, DbDep, ManagerDep
from argus_api.inbox import InboxPage
from argus_api.paging import DEFAULT_LIMIT, View

router = APIRouter(tags=["decisions"])


@router.get("/inbox")
async def inbox(
    request: Request,
    db: DbDep,
    caller: ManagerDep,
    project: str | None = None,
    state: Annotated[list[TaskState] | None, Query()] = None,
    decision_kind: Annotated[list[DecisionKind] | None, Query()] = None,
    blocking_only: bool = False,
    order: inbox_module.Order = "age",
    view: inbox_module.InboxView = "summary",
    forge: bool = False,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> inbox_actions.ActionableInboxPage | InboxPage:
    """What waits on a manager, oldest first (`order=impact`: most blocked tasks
    first) and grouped by project and task (`groups`): blocked, in-review and
    awaiting-approval tasks with their open decision, pending tasks stuck behind a
    failed or cancelled dependency (`failed_dependencies`), plus open decisions
    about pools and knowledge. `state` keeps only task rows in those states;
    `blocking_only` keeps rows that block at least one task (`blocks`, `impact`).
    `view=actionable` adds to each row what acting on it takes (authority, effects,
    impact, evidence, a suggested next action; advisory only); `forge=true` adds
    bounded live PR reads to it, outside the snapshot."""
    if view == "actionable":
        return await inbox_actions.actionable_inbox(
            db,
            request.app.state.pr_checker,
            caller.scope,
            forge=forge,
            project=project,
            states=state,
            decision_kinds=decision_kind,
            blocking_only=blocking_only,
            order=order,
            limit=limit,
            cursor=cursor,
        )
    async with db.snapshot() as snap:
        return await inbox_module.inbox(
            snap,
            project=project,
            states=state,
            decision_kinds=decision_kind,
            blocking_only=blocking_only,
            order=order,
            view=view,
            limit=limit,
            cursor=cursor,
        )


@router.get("/decisions")
async def list_decisions(
    db: DbDep,
    caller: CallerDep,
    state: DecisionState | None = DecisionState.OPEN,
    subject_id: str | None = None,
    project: str | None = None,
    kind: Annotated[list[DecisionKind] | None, Query()] = None,
    view: View = "summary",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> DecisionPage:
    """Decisions oldest first, in pages (max 100); open ones by default.
    Workers only see decisions for tasks they have leased (and knowledge
    proposals in those projects)."""
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
            decision_scope=worker_decision_filter(caller),
        )


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str, db: DbDep, caller: CallerDep) -> Decision:
    async with db.read() as conn:
        await require_decision_readable(conn, caller, decision_id)
        return await store.get(conn, decision_id)


@router.get("/decisions/{decision_id}/preview")
async def preview_decision(
    decision_id: str,
    db: DbDep,
    caller: ManagerDep,
    choice: str | None = None,
    text: Annotated[str, Query(max_length=10_000)] = "",
    request_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> preview_module.DecisionPreview:
    """What answering would do, read-only and advisory (it authorizes and executes
    nothing). Takes the answer's `choice`, `text` and `request_id`; reports the
    verdict, transition and effects the answer path would produce, and what stored
    observations say about the decision's premise. A snapshot: compare
    `snapshot.decision_version` to see staleness."""
    async with db.snapshot() as snap:
        return await preview_module.preview(
            snap, decision_id, caller, choice=choice, text=text, request_id=request_id
        )


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
