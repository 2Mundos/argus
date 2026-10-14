import asyncio
from typing import Any

from argus_core.auth import Scope
from argus_core.tasks import Task, TaskCreate, TaskState
from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from argus_api.deps import AgentDep, CallerDep, DbDep, ManagerDep
from argus_api.events import Event
from argus_api.tasks import service

router = APIRouter(prefix="/tasks", tags=["tasks"])


class ClaimRequest(BaseModel):
    capabilities: list[str] = []
    wait_s: float = Field(default=0, ge=0, le=30)


class CompleteRequest(BaseModel):
    attempt: int
    result: dict[str, Any] = {}


class FailRequest(BaseModel):
    attempt: int
    error: str = Field(min_length=1)


class BlockRequest(BaseModel):
    attempt: int
    reason: str = Field(min_length=1)


class ReviewRequest(BaseModel):
    accept: bool
    notes: str = ""


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreate, db: DbDep, caller: CallerDep) -> Task:
    """Orchestrators and admins create any task; workers only subtasks of a task
    they hold."""
    as_worker = caller.agent_id if caller.scope is Scope.WORKER else None
    async with db.write() as tx:
        return await service.create_task(tx, body, caller.actor, as_worker=as_worker)


@router.get("")
async def list_tasks(
    db: DbDep, _: CallerDep, state: TaskState | None = None, limit: int = 100
) -> list[Task]:
    async with db.read() as conn:
        return await service.list_tasks(conn, state=state, limit=min(limit, 500))


@router.post(
    "/claim",
    response_model=Task,
    responses={204: {"description": "No matching task became ready within wait_s"}},
)
async def claim_task(
    body: ClaimRequest, db: DbDep, agent_id: AgentDep
) -> Task | Response:
    """Claim a ready task, long-polling up to `wait_s` seconds for one."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + body.wait_s
    with db.bus.subscribe() as sub:
        while True:
            async with db.write() as tx:
                task = await service.claim(tx, agent_id, body.capabilities)
            if task is not None:
                return task
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    await sub.next_matching(_became_ready)
            except TimeoutError:
                return Response(status_code=status.HTTP_204_NO_CONTENT)


def _became_ready(event: Event) -> bool:
    return event.subject_type == "task" and event.data.get("to") == TaskState.READY


@router.get("/{task_id}")
async def get_task(task_id: str, db: DbDep, _: CallerDep) -> Task:
    async with db.read() as conn:
        return await service.get_task(conn, task_id)


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: str, body: CompleteRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    async with db.write() as tx:
        return await service.complete(tx, agent_id, task_id, body.attempt, body.result)


@router.post("/{task_id}/fail")
async def fail_task(
    task_id: str, body: FailRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    async with db.write() as tx:
        return await service.fail(tx, agent_id, task_id, body.attempt, body.error)


@router.post("/{task_id}/block")
async def block_task(
    task_id: str, body: BlockRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    async with db.write() as tx:
        return await service.block(tx, agent_id, task_id, body.attempt, body.reason)


@router.post("/{task_id}/unblock")
async def unblock_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    async with db.write() as tx:
        return await service.unblock(tx, task_id, caller.actor)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    async with db.write() as tx:
        return await service.cancel(tx, task_id, caller.actor)


@router.post("/{task_id}/approve")
async def approve_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    async with db.write() as tx:
        return await service.approve(tx, task_id, caller.actor)


@router.post("/{task_id}/review")
async def review_task(
    task_id: str, body: ReviewRequest, db: DbDep, caller: ManagerDep
) -> Task:
    """Tasks with `review: human` need an admin; `orchestrator` tasks accept either."""
    async with db.write() as tx:
        return await service.review(
            tx,
            task_id,
            caller.actor,
            caller.scope,
            accept=body.accept,
            notes=body.notes,
        )
