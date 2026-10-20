import asyncio
from datetime import timedelta
from typing import Annotated, Any, Literal

from argus_core.auth import Scope
from argus_core.tasks import (
    ClaimedTask,
    Task,
    TaskCreate,
    TaskResult,
    TaskState,
    validate_milestone,
)
from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field, model_validator

from argus_api.db import utcnow
from argus_api.deps import AdminDep, AgentDep, CallerDep, DbDep, ManagerDep, SettingsDep
from argus_api.errors import ValidationFailedError
from argus_api.events import Event, OverflowedError
from argus_api.knowledge import service as knowledge
from argus_api.paging import DEFAULT_LIMIT, View
from argus_api.tasks import service, views
from argus_api.tasks.views import TaskPage

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
    # A milestone (progress-milestones spec 3.1): a kind from the closed set and its data.
    kind: str | None = None
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _milestone(self) -> "ProgressRequest":
        validate_milestone(self.kind, self.data)
        return self


class FailRequest(BaseModel):
    attempt: int
    error: str = Field(min_length=1)
    reason: Literal["worker_fail", "channel_error", "provider_failure"] = "worker_fail"
    """Why the attempt failed: the worker said so, or the channel detected it."""


class DependenciesRequest(BaseModel):
    depends_on: list[str] = Field(max_length=service.MAX_DEPENDENCIES)
    expected_version: str = Field(min_length=1)  # the task's `version`


class BulkArchiveRequest(BaseModel):
    """Which terminal tasks to archive: all of `states`, narrowed by project and
    by how long ago they last changed. `dry_run` only counts."""

    states: list[TaskState] = Field(min_length=1)
    project: str | None = None
    older_than_days: int | None = Field(default=None, ge=0)
    dry_run: bool = False


class BulkArchiveResult(BaseModel):
    count: int  # tasks archived, or that would be with `dry_run`
    dry_run: bool


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
    db: DbDep,
    _: CallerDep,
    state: Annotated[list[TaskState] | None, Query()] = None,
    project: str | None = None,
    kind: str | None = None,
    assignee: str | None = None,
    archived: bool = False,
    ids: str | None = None,
    view: View = "summary",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> TaskPage:
    """Tasks newest first, in pages (max 100). `assignee` is the agent holding a
    task now (claimed tasks only). `ids` (comma-separated, max 50) is a batch read
    instead: no other filter, no paging, unknown ids in `missing`. Archived tasks
    are left out unless `archived=true`, which lists only those."""
    async with db.snapshot() as snap:
        if ids is not None:
            if state or project or kind or assignee or archived or cursor:
                raise ValidationFailedError(
                    "ids is a batch read; it takes no other filter or cursor",
                    code="ids_exclusive",
                )
            return await views.get_tasks(snap, ids.split(","), view)
        return await views.list_tasks(
            snap,
            states=state,
            project=project,
            kind=kind,
            assignee=assignee,
            archived=archived,
            view=view,
            limit=limit,
            cursor=cursor,
        )


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
            tx,
            agent_id,
            task_id,
            body.attempt,
            body.message,
            body.percent,
            body.kind,
            body.data,
        )


@router.post("/{task_id}/fail")
async def fail_task(
    task_id: str, body: FailRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    async with db.write() as tx:
        return await service.fail(
            tx, agent_id, task_id, body.attempt, body.error, body.reason
        )


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


@router.post("/{task_id}/retry")
async def retry_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    """Return a `failed` task to `ready` with a fresh window of attempts (409 otherwise)."""
    async with db.write() as tx:
        return await service.retry(tx, task_id, caller.actor)


@router.put("/{task_id}/dependencies")
async def set_dependencies(
    task_id: str, body: DependenciesRequest, db: DbDep, caller: ManagerDep
) -> Task:
    """Replace the dependencies of a task that is still waiting (pending,
    awaiting_approval or ready). A ready task with an unfinished new dependency
    goes back to pending; a pending one whose dependencies all succeeded becomes
    ready. Refuses cycles (422 dependency_cycle) and stale versions (409)."""
    async with db.write() as tx:
        return await service.set_dependencies(
            tx, task_id, body.depends_on, body.expected_version, caller.actor
        )


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    async with db.write() as tx:
        return await service.cancel(tx, task_id, caller.actor)


@router.post("/archive")
async def archive_tasks(
    body: BulkArchiveRequest, db: DbDep, caller: ManagerDep
) -> BulkArchiveResult:
    """Archive terminal tasks by state, project and age, answering how many.
    `dry_run` counts without writing, for a confirmation. Non-terminal states
    are refused (422 `state_not_archivable`) before anything changes."""
    async with db.write() as tx:
        count = await service.archive_matching(
            tx,
            caller.actor,
            states=body.states,
            project=body.project,
            older_than_days=body.older_than_days,
            dry_run=body.dry_run,
        )
    return BulkArchiveResult(count=count, dry_run=body.dry_run)


@router.post("/{task_id}/archive")
async def archive_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    """Archive a `succeeded`, `failed` or `cancelled` task; repeating it changes
    nothing. Any other state answers 409 `task_not_terminal` and changes nothing."""
    async with db.write() as tx:
        return await service.archive(tx, task_id, caller.actor)


@router.post("/{task_id}/unarchive")
async def unarchive_task(task_id: str, db: DbDep, caller: AdminDep) -> Task:
    """Return an archived task to listings and totals (admin). Idempotent."""
    async with db.write() as tx:
        return await service.unarchive(tx, task_id, caller.actor)
