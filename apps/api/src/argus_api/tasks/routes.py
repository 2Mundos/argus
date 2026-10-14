import asyncio
from datetime import timedelta

from argus_core.auth import Scope
from argus_core.tasks import ClaimedTask, Task, TaskCreate, TaskResult, TaskState
from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from argus_api.db import utcnow
from argus_api.deps import AgentDep, CallerDep, DbDep, ManagerDep, SettingsDep
from argus_api.events import Event, OverflowedError
from argus_api.knowledge import service as knowledge
from argus_api.tasks import service

router = APIRouter(prefix="/tasks", tags=["tasks"])


class ClaimRequest(BaseModel):
    capabilities: list[str] = []
    wait_s: float = Field(default=0, ge=0, le=30)


class CompleteRequest(BaseModel):
    attempt: int
    result: TaskResult


class ProgressRequest(BaseModel):
    attempt: int
    message: str = Field(min_length=1, max_length=2000)
    percent: int | None = Field(default=None, ge=0, le=100)


class FailRequest(BaseModel):
    attempt: int
    error: str = Field(min_length=1)


class BlockRequest(BaseModel):
    attempt: int
    reason: str = Field(min_length=1)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate, db: DbDep, caller: CallerDep, settings: SettingsDep
) -> Task:
    """Orchestrators and admins create any task; workers only subtasks of a task
    they hold. Past the per-agent hourly limit, an agent's new tasks wait for
    human approval (a `policy` decision) instead of being refused."""
    as_worker = caller.agent_id if caller.scope is Scope.WORKER else None
    limit = settings.max_tasks_per_agent_hour
    hourly_limit = limit if caller.agent_id is not None and limit > 0 else None
    async with db.write() as tx:
        return await service.create_task(
            tx, body, caller.actor, as_worker=as_worker, hourly_limit=hourly_limit
        )


@router.get("")
async def list_tasks(
    db: DbDep, _: CallerDep, state: TaskState | None = None, limit: int = 100
) -> list[Task]:
    async with db.read() as conn:
        return await service.list_tasks(conn, state=state, limit=min(limit, 500))


@router.post(
    "/claim",
    response_model=ClaimedTask,
    responses={204: {"description": "No matching task became ready within wait_s"}},
)
async def claim_task(
    body: ClaimRequest, db: DbDep, agent_id: AgentDep, settings: SettingsDep
) -> ClaimedTask | Response:
    """Claim a ready task, long-polling up to `wait_s` seconds for one."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + body.wait_s
    with db.bus.subscribe() as sub:
        while True:
            async with db.write() as tx:
                task = await service.claim(
                    tx,
                    agent_id,
                    body.capabilities,
                    default_timeout_s=settings.task_timeout_s,
                )
            if task is not None:
                async with db.read() as conn:
                    refs = await knowledge.refs_for_task(
                        conn,
                        task,
                        now=utcnow(),
                        stale_after=timedelta(days=settings.knowledge_stale_days),
                    )
                return ClaimedTask(**task.model_dump(), knowledge_refs=refs)
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    await sub.next_matching(_became_ready)
            except OverflowedError:
                continue  # missed events: just try to claim again
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


@router.post("/{task_id}/progress")
async def report_progress(
    task_id: str, body: ProgressRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    """Post a progress note (at most one per 10 s). Also renews the lease."""
    async with db.write() as tx:
        return await service.report_progress(
            tx, agent_id, task_id, body.attempt, body.message, body.percent
        )


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
