from typing import Annotated

from argus_core.kinds import KIND_NAME_PATTERN, TaskKind, TaskKindUpsert
from fastapi import APIRouter, Path

from argus_api.deps import CallerDep, DbDep, ManagerDep
from argus_api.kinds import service

router = APIRouter(prefix="/task-kinds", tags=["task-kinds"])

KindName = Annotated[str, Path(pattern=KIND_NAME_PATTERN)]


@router.get("")
async def list_kinds(db: DbDep, _: CallerDep) -> list[TaskKind]:
    async with db.read() as conn:
        return await service.list_kinds(conn)


@router.get("/{name}")
async def get_kind(name: KindName, db: DbDep, _: CallerDep) -> TaskKind:
    async with db.read() as conn:
        return await service.get_kind(conn, name)


@router.put("/{name}")
async def upsert_kind(
    name: KindName, body: TaskKindUpsert, db: DbDep, caller: ManagerDep
) -> TaskKind:
    """Create or replace a kind. Existing tasks keep the values they were created
    with; schemas apply to new tasks and to results submitted from now on."""
    async with db.write() as tx:
        return await service.upsert_kind(tx, name, body, caller.actor)
