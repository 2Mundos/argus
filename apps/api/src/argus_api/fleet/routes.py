from typing import Annotated

from argus_core.fleet import (
    POOL_NAME_PATTERN,
    Instance,
    Pool,
    PoolUpsert,
    ScaleRequest,
    ScaleResult,
)
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import PlainTextResponse

from argus_api.deps import AdminDep, DbDep, ManagerDep
from argus_api.errors import DomainError
from argus_api.fleet import service
from argus_api.fleet.targets import TargetError, TargetRegistry

router = APIRouter(tags=["fleet"])

PoolName = Annotated[str, Path(pattern=POOL_NAME_PATTERN)]


class TargetUnavailableError(DomainError):
    status = 502
    code = "target_unavailable"


@router.get("/pools")
async def list_pools(db: DbDep, _: ManagerDep) -> list[Pool]:
    async with db.read() as conn:
        return await service.list_pools(conn)


@router.get("/pools/{name}")
async def get_pool(name: PoolName, db: DbDep, _: ManagerDep) -> Pool:
    async with db.read() as conn:
        return await service.get_pool(conn, name)


@router.put("/pools/{name}")
async def upsert_pool(
    name: PoolName, body: PoolUpsert, db: DbDep, caller: AdminDep
) -> Pool:
    """Create or replace a pool definition, including its replica cap (admin only).
    The reconciler converges instances to it within seconds."""
    async with db.write() as tx:
        return await service.upsert_pool(tx, name, body, caller.actor)


@router.post("/pools/{name}/scale")
async def scale_pool(
    name: PoolName, body: ScaleRequest, db: DbDep, caller: ManagerDep
) -> ScaleResult:
    """Set desired replicas. Orchestrators may scale up to `max_replicas`; asking
    for more opens a decision for a human (`decision_id`) instead of failing."""
    async with db.write() as tx:
        pool, decision_id = await service.scale(
            tx, name, body.desired_replicas, caller.actor, caller.scope
        )
    return ScaleResult(pool=pool, decision_id=decision_id)


@router.get("/instances")
async def list_instances(
    db: DbDep, _: ManagerDep, pool: str | None = None, include_terminated: bool = False
) -> list[Instance]:
    async with db.read() as conn:
        return await service.list_instances(conn, pool, include_terminated)


@router.get("/instances/{instance_id}")
async def get_instance(instance_id: str, db: DbDep, _: ManagerDep) -> Instance:
    async with db.read() as conn:
        return await service.get_instance(conn, instance_id)


@router.post("/instances/{instance_id}/terminate")
async def terminate_instance(instance_id: str, db: DbDep, caller: AdminDep) -> Instance:
    """Stop an instance now: its agents end immediately (tasks released); its slot
    frees once the provider confirms the container is gone."""
    async with db.write() as tx:
        await service.request_termination(
            tx, instance_id, f"terminated by {caller.actor}"
        )
    async with db.read() as conn:
        return await service.get_instance(conn, instance_id)


@router.get("/instances/{instance_id}/logs", response_class=PlainTextResponse)
async def instance_logs(
    instance_id: str,
    request: Request,
    db: DbDep,
    _: ManagerDep,
    tail: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> PlainTextResponse:
    """The container's recent output, read live from the target (not stored)."""
    async with db.read() as conn:
        instance = await service.get_instance(conn, instance_id)
        pool = await service.get_pool(conn, instance.pool)
    targets: TargetRegistry = request.app.state.targets
    try:
        text = await targets.get(pool.target, pool.docker.host).logs(
            instance.provider_ref, tail=tail
        )
    except TargetError as exc:
        raise TargetUnavailableError(str(exc)) from exc
    return PlainTextResponse(text)
