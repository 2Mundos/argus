"""REST routes for routines (docs/specs/routines.spec.md 3.4). Any token reads
(workers within their leased projects); orchestrators and admins manage."""

from typing import Annotated

from argus_core.auth import Scope
from argus_core.knowledge import SLUG_PATTERN
from argus_core.routines import (
    Routine,
    RoutineEnabled,
    RoutineRunResult,
    RoutineWrite,
)
from fastapi import APIRouter, Path, Response, status

from argus_api.access import require_project_readable
from argus_api.deps import CallerDep, DbDep, ManagerDep
from argus_api.routines import runs, service

router = APIRouter(tags=["routines"])
Slug = Annotated[str, Path(pattern=SLUG_PATTERN)]


@router.get("/projects/{slug}/routines")
async def list_routines(slug: Slug, db: DbDep, caller: CallerDep) -> list[Routine]:
    async with db.read() as conn:
        await require_project_readable(conn, caller, slug)
        return await service.list_routines(conn, slug, db.now())


@router.get("/routines/{routine_id}")
async def get_routine(routine_id: str, db: DbDep, caller: CallerDep) -> Routine:
    """The routine, its next three slots and its most recent run tasks."""
    async with db.read() as conn:
        routine = await service.get_routine(conn, routine_id, db.now())
        await require_project_readable(conn, caller, routine.project)
        return routine


@router.put("/projects/{slug}/routines/{routine}")
async def put_routine(
    slug: Slug, routine: Slug, body: RoutineWrite, db: DbDep, caller: ManagerDep
) -> Routine:
    """Create or replace. `findings: auto`, or changing an `auto` routine, is admin only."""
    async with db.write() as tx:
        return await service.upsert(
            tx, slug, routine, body, caller.actor, admin=caller.scope == Scope.ADMIN
        )


@router.patch("/projects/{slug}/routines/{routine}")
async def patch_routine(
    slug: Slug, routine: Slug, body: RoutineEnabled, db: DbDep, caller: ManagerDep
) -> Routine:
    async with db.write() as tx:
        return await service.set_enabled(
            tx,
            slug,
            routine,
            body.enabled,
            caller.actor,
            admin=caller.scope == Scope.ADMIN,
        )


@router.delete(
    "/projects/{slug}/routines/{routine}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_routine(
    slug: Slug, routine: Slug, db: DbDep, caller: ManagerDep
) -> Response:
    """Remove the routine; its run tasks stay."""
    async with db.write() as tx:
        await service.delete(tx, slug, routine, caller.actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/routines/{routine_id}/run")
async def run_routine(
    routine_id: str, db: DbDep, caller: ManagerDep
) -> RoutineRunResult:
    """Run it now, even while disabled or paused. If its previous run is still
    open, nothing is created: `skipped: previous_running` (a `routine.skipped`)."""
    async with db.write() as tx:
        return await runs.run_now(tx, routine_id, caller.actor)
