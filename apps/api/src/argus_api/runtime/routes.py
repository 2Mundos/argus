"""Project runtime REST (docs/specs/project-runtime.spec.md). The MCP tools and
the dashboard call these; nothing else writes a runtime declaration."""

from typing import Annotated

from argus_core.knowledge import SLUG_PATTERN
from argus_core.runtime import (
    CATALOG,
    HoldRequest,
    PrepareRequest,
    RuntimeSpec,
    RuntimeView,
    Selection,
    UsedRequest,
    UsedResult,
)
from fastapi import APIRouter, Path, Request

from argus_api.access import require_project_readable
from argus_api.deps import AdminDep, AgentDep, CallerDep, DbDep
from argus_api.errors import ConflictError
from argus_api.runtime import service
from argus_api.runtime.service import RuntimeManager

router = APIRouter(tags=["runtime"])
Slug = Annotated[str, Path(pattern=SLUG_PATTERN)]


def _manager(request: Request) -> RuntimeManager:
    return request.app.state.runtime


@router.get("/runtime/catalog")
async def catalog(_: CallerDep) -> list[dict]:
    """The approved components and their pins: all a project may declare."""
    return [
        {"id": c.id, "kind": c.kind, "versions": list(c.versions), "purpose": c.purpose}
        for c in CATALOG.values()
    ]


@router.get("/projects/{slug}/runtime")
async def get_project_runtime(slug: Slug, db: DbDep, caller: CallerDep) -> RuntimeView:
    async with db.read() as conn:
        await require_project_readable(conn, caller, slug)
        return await service.view(conn, slug)


@router.put("/projects/{slug}/runtime")
async def put_project_runtime(
    slug: Slug, body: RuntimeSpec, db: DbDep, caller: AdminDep
) -> RuntimeView:
    """Declare the project's runtime (admin only; versioned by the event log)."""
    async with db.write() as tx:
        await service.put_spec(tx, slug, body, caller.actor)
    async with db.read() as conn:
        return await service.view(conn, slug)


@router.post("/projects/{slug}/runtime/rebuild")
async def rebuild_project_runtime(
    slug: Slug, db: DbDep, caller: AdminDep, request: Request
) -> RuntimeView:
    """Retry the failed builds of the project's current recipe."""
    async with db.write() as tx:
        await service.rebuild(tx, slug, caller.actor)
    _manager(request).wake()
    async with db.read() as conn:
        return await service.view(conn, slug)


@router.post("/runtime/prepare")
async def prepare(
    body: PrepareRequest, request: Request, agent_id: AgentDep
) -> Selection:
    """The claiming worker asks for its task project's image before any model
    work. Anything but `ready` has already held the task (no attempt spent)."""
    return await _manager(request).prepare(agent_id, body)


@router.post("/runtime/hold", status_code=204)
async def hold(body: HoldRequest, request: Request, agent_id: AgentDep) -> None:
    """The worker could not prepare its environment: hold the claimed task, durably
    and without spending an attempt (`runtime_failed`, with `error`)."""
    await _manager(request).hold(agent_id, body)


@router.post("/runtime/used")
async def used(body: UsedRequest, request: Request, agent_id: AgentDep) -> UsedResult:
    result = await _manager(request).used(agent_id, body)
    if not result.verified:
        raise ConflictError(
            f"session ran image {result.used_image_id}, not the selected {result.selected_image_id};"
            " the task was held",
            code="runtime_image_mismatch",
        )
    return result


@router.get("/runtime/tasks/{task_id}")
async def task_runtime(task_id: str, db: DbDep, _: CallerDep) -> list[dict]:
    """Selected versus actually used image of each attempt of the task."""
    async with db.read() as conn:
        return await service.task_runtime(conn, task_id)
