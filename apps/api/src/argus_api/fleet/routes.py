from typing import Annotated

from argus_core.auth import Scope
from argus_core.fleet import (
    POOL_NAME_PATTERN,
    TEMPLATE_DEFAULT_MAX_REPLICAS,
    CreateWorkerRequest,
    CreateWorkerResult,
    FleetCapabilities,
    Instance,
    LaunchRequest,
    LaunchResult,
    Pool,
    PoolUpsert,
    ScaleRequest,
    ScaleResult,
    TemplateCatalog,
)
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import PlainTextResponse

from argus_api.deps import AdminDep, DbDep, ManagerDep, SettingsDep
from argus_api.errors import DomainError, ForbiddenError
from argus_api.fleet import capabilities, runtimes, service, templates
from argus_api.fleet.targets import TargetError, TargetRegistry

router = APIRouter(tags=["fleet"])

PoolName = Annotated[str, Path(pattern=POOL_NAME_PATTERN)]


class TargetUnavailableError(DomainError):
    status = 502
    code = "target_unavailable"


class NotLaunchableError(DomainError):
    status = 409
    code = "not_launchable"


@router.get("/fleet/capabilities")
async def fleet_capabilities(
    request: Request, db: DbDep, settings: SettingsDep, _: ManagerDep
) -> FleetCapabilities:
    """What this node can run right now, measured from its targets (read-only)."""
    async with db.read() as conn:
        pools = await service.list_pools(conn)
    return await capabilities.discover(
        pools, request.app.state.targets, settings.public_url
    )


@router.get("/fleet/templates")
async def worker_templates(
    request: Request, db: DbDep, settings: SettingsDep, _: ManagerDep
) -> TemplateCatalog:
    """The worker templates Argus supports, per node: ready, or exactly what is
    missing. Read-only; nothing is created by looking."""
    async with db.read() as conn:
        pools = await service.list_pools(conn)
    return await templates.catalog(settings, pools, request.app.state.targets)


@router.post("/fleet/workers", status_code=201)
async def create_worker(
    body: CreateWorkerRequest,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    caller: ManagerDep,
) -> CreateWorkerResult:
    """Create a pool from a template and request its first workers, in one
    confirmed call. The pool is rebuilt server-side from the template; refusals
    happen before anything is written. Success means *requested*."""
    if caller.scope is not Scope.ADMIN:
        if body.advanced is not None:
            raise ForbiddenError("advanced options need an admin token")
        if body.quantity > TEMPLATE_DEFAULT_MAX_REPLICAS:
            raise ForbiddenError(
                f"orchestrators may create up to {TEMPLATE_DEFAULT_MAX_REPLICAS}"
                " workers at once; ask an admin for more"
            )
    async with db.read() as conn:
        pools = await service.list_pools(conn)
    # Probed before the write: no target call inside a write transaction.
    cat = await templates.catalog(settings, pools, request.app.state.targets)
    name, upsert = templates.build_pool(settings, cat, body, pools)
    async with db.write() as tx:
        pool = await service.create_pool(tx, name, upsert, caller.actor)
    return CreateWorkerResult(
        pool=pool, requested=body.quantity, desired_replicas=pool.desired_replicas
    )


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


@router.post("/pools/{name}/launch")
async def launch_worker(
    name: PoolName,
    body: LaunchRequest,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    caller: ManagerDep,
) -> LaunchResult:
    """Request one more worker from the pool. Refused (nothing changes) when the
    node cannot start it now. Success means *requested*: the reconciler starts
    the instance next pass; follow it in `/instances`."""
    async with db.read() as conn:
        pool = await service.get_pool(conn, name)
    # Probed before the write: no target call inside a write transaction.
    verdict = await capabilities.discover_pool(
        pool, request.app.state.targets, settings.public_url
    )
    # A full pool is reported by the write below as `pool_at_capacity`.
    blockers = [
        c.detail for c in verdict.checks if c.ok is False and c.name != "capacity"
    ]
    if blockers:
        raise NotLaunchableError(
            f"pool {name!r} cannot start a worker now", details=blockers
        )
    async with db.write() as tx:
        pool = await service.launch(
            tx, name, body.expected_desired_replicas, caller.actor
        )
    return LaunchResult(
        pool=pool, requested=True, desired_replicas=pool.desired_replicas
    )


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
        text = await targets.get(pool.target, *runtimes.target_location(pool)).logs(
            instance.provider_ref, tail=tail
        )
    except TargetError as exc:
        raise TargetUnavailableError(str(exc)) from exc
    return PlainTextResponse(text)
