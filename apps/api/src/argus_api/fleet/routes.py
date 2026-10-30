import asyncio
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
    PoolInspection,
    PoolPatch,
    PoolUpsert,
    ScaleRequest,
    ScaleResult,
    TemplateCatalog,
    WorkspaceCleanupPage,
    WorkspaceCleanupRunRequest,
    WorkspaceCleanupStatus,
    WorkspaceCleanupSummary,
)
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from argus_api.deps import AdminDep, DbDep, ManagerDep, SettingsDep
from argus_api.errors import DomainError, ForbiddenError
from argus_api.fleet import (
    capabilities,
    policy,
    runtimes,
    service,
    templates,
    workspace_cleanup,
)
from argus_api.fleet.targets import TargetError, TargetRegistry
from argus_api.fleet.workspace_cleanup import WorkspaceCleaner
from argus_api.orchestrator import require_not_internal_orchestrator

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
    policy.enforce_nanoclaw_image_roles(upsert, settings)
    policy.enforce_nanoclaw_socket_policy(upsert, settings)
    async with db.read() as conn:
        await require_not_internal_orchestrator(conn, caller, action="create_worker")
    async with db.write() as tx:
        pool = await service.create_pool(tx, name, upsert, caller.actor)
    return CreateWorkerResult(
        pool=service.redact(pool),
        requested=body.quantity,
        desired_replicas=pool.desired_replicas,
    )


@router.get("/pools")
async def list_pools(db: DbDep, _: ManagerDep) -> list[Pool]:
    """Every pool; `env` shows its keys with redacted values."""
    async with db.read() as conn:
        return [service.redact(p) for p in await service.list_pools(conn)]


@router.get("/pools/{name}")
async def get_pool(
    name: PoolName, db: DbDep, caller: ManagerDep, reveal_env: bool = False
) -> Pool:
    """A pool; `env` values are redacted unless an admin asks for `reveal_env`."""
    if reveal_env and caller.scope is not Scope.ADMIN:
        raise ForbiddenError("reveal_env needs an admin token")
    async with db.read() as conn:
        pool = await service.get_pool(conn, name)
    return pool if reveal_env else service.redact(pool)


@router.put("/pools/{name}")
async def upsert_pool(
    name: PoolName,
    body: PoolUpsert,
    db: DbDep,
    settings: SettingsDep,
    caller: AdminDep,
) -> Pool:
    """Create or replace a pool definition, including its replica cap (admin only).
    The reconciler converges instances to it within seconds. This replaces `env`
    whole; PATCH changes single variables without resending the others."""
    policy.enforce_nanoclaw_image_roles(body, settings)
    policy.enforce_nanoclaw_socket_policy(body, settings)
    from argus_api.errors import NotFoundError

    async with db.read() as conn:
        try:
            existing = await service.get_pool(conn, name)
        except NotFoundError:
            existing = None
        if existing is not None:
            service.require_not_system(existing, action="replace")
    async with db.write() as tx:
        return service.redact(await service.upsert_pool(tx, name, body, caller.actor))


@router.patch("/pools/{name}")
async def patch_pool(
    name: PoolName,
    body: PoolPatch,
    db: DbDep,
    settings: SettingsDep,
    caller: AdminDep,
) -> Pool:
    """Change some of a pool's options (admin only), keeping the rest. Requires
    the `version` you read as `expected_version` (409 stale_version otherwise).
    Name, target and runtime cannot change. `env` merges; null removes a key."""
    # Policy runs after merge inside patch_pool so a PATCH that introduces a
    # socket-mounted NanoClaw config is refused the same way as PUT.
    async with db.write() as tx:
        return service.redact(
            await service.patch_pool(tx, name, body, caller.actor, settings)
        )


@router.get("/pools/{name}/inspect")
async def inspect_pool(name: PoolName, db: DbDep, _: ManagerDep) -> PoolInspection:
    """The pool, its live instances and agents, and per instance what is proven
    about what runs there: each fact says its source (configured, requested,
    started, validated) or `unknown`."""
    async with db.snapshot() as snap:
        return await service.inspect(snap.conn, name)


@router.post("/pools/{name}/scale")
async def scale_pool(
    name: PoolName, body: ScaleRequest, db: DbDep, caller: ManagerDep
) -> ScaleResult:
    """Set desired replicas. Orchestrators may scale up to `max_replicas`; asking
    for more opens a decision for a human (`decision_id`) instead of failing."""
    async with db.read() as conn:
        await require_not_internal_orchestrator(conn, caller, action="scale_pool")
    async with db.write() as tx:
        pool, decision_id = await service.scale(
            tx, name, body.desired_replicas, caller.actor, caller.scope
        )
    return ScaleResult(pool=service.redact(pool), decision_id=decision_id)


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
    service.require_launch_precondition(
        body.expected_desired_replicas, body.unconditional
    )
    async with db.read() as conn:
        await require_not_internal_orchestrator(conn, caller, action="launch_worker")
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
        pool=service.redact(pool),
        requested=True,
        desired_replicas=pool.desired_replicas,
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


class TerminateRequest(BaseModel):
    requeue: bool = True
    """Release the tasks its agents hold without counting a failed attempt.
    False expires them as failures (a worker that keeps breaking on its task)."""


class RecycleRequest(BaseModel):
    requeue: bool = False
    """Release the tasks its agents hold now (no failed attempt) instead of
    waiting for them to finish, so the instance stops on the next pass."""


@router.post("/instances/{instance_id}/terminate")
async def terminate_instance(
    instance_id: str,
    db: DbDep,
    caller: AdminDep,
    body: TerminateRequest | None = None,
) -> Instance:
    """Stop an instance now: its agents end immediately and the tasks they hold are
    requeued without counting an attempt (unless `requeue` is false); its slot
    frees once the provider confirms the container is gone."""
    body = body or TerminateRequest()
    async with db.write() as tx:
        await service.request_termination(
            tx, instance_id, caller.actor, requeue=body.requeue
        )
    async with db.read() as conn:
        return await service.get_instance(conn, instance_id)


@router.post("/instances/{instance_id}/recycle")
async def recycle_instance(
    instance_id: str,
    db: DbDep,
    caller: ManagerDep,
    body: RecycleRequest | None = None,
) -> Instance:
    """Replace a running instance: its agents drain, it stops once none holds a
    claimed task, and a new one starts in its place. By default work finishes
    first; with `requeue` the claimed tasks are released now, no attempt counted."""
    body = body or RecycleRequest()
    if body.requeue:
        async with db.read() as conn:
            await require_not_internal_orchestrator(
                conn, caller, action="recycle_instance with requeue"
            )
    async with db.write() as tx:
        await service.recycle(tx, instance_id, caller.actor, requeue=body.requeue)
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


class WorkspaceCleanupDisabledError(DomainError):
    status = 409
    code = "workspace_cleanup_disabled"


class ArchiveRootInvalidError(DomainError):
    status = 409
    code = "archive_root_invalid"


@router.get("/fleet/workspace-cleanup")
async def workspace_cleanup_summary(
    request: Request, db: DbDep, settings: SettingsDep, _: ManagerDep
) -> WorkspaceCleanupSummary:
    """Settings, last run, per-status counts, estimated vs freed bytes, and the
    latest blocked or failed directories of the workspace cleanup."""
    cleaner: WorkspaceCleaner = request.app.state.workspace_cleanup
    async with db.read() as conn:
        return await workspace_cleanup.summary(conn, settings, cleaner.busy)


@router.get("/fleet/workspace-cleanup/instances")
async def workspace_cleanup_entries(
    db: DbDep,
    _: ManagerDep,
    status: WorkspaceCleanupStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkspaceCleanupPage:
    """Per-directory audit rows, most recently updated first."""
    async with db.read() as conn:
        return await workspace_cleanup.list_entries(conn, status, limit, offset)


@router.post("/fleet/workspace-cleanup/run")
async def run_workspace_cleanup(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    caller: ManagerDep,
    body: WorkspaceCleanupRunRequest | None = None,
) -> WorkspaceCleanupSummary:
    """Start a cleanup run now and wait up to `wait_s` for it; the summary shows
    its progress. A dry run (the default) deletes nothing and works while cleanup
    is disabled; `dry_run: false` needs an admin token, cleanup enabled and a
    valid archive root."""
    body = body or WorkspaceCleanupRunRequest()
    if not body.dry_run:
        if caller.scope is not Scope.ADMIN:
            raise ForbiddenError("a real workspace cleanup needs an admin token")
        if not settings.workspace_cleanup_enabled:
            raise WorkspaceCleanupDisabledError("workspace cleanup is disabled")
        async with db.read() as conn:
            pools = await service.list_pools(conn)
        roots = [p.nanoclaw.instances_root for p in pools if p.nanoclaw]
        problem = workspace_cleanup.check_archive_root(
            settings.workspace_cleanup_archive_root, roots
        )
        if problem:
            raise ArchiveRootInvalidError(
                "the archive root is unset or invalid", code=problem
            )
    cleaner: WorkspaceCleaner = request.app.state.workspace_cleanup
    task = cleaner.start("manual", caller.actor, body.dry_run)
    if body.wait_s:
        await asyncio.wait({task}, timeout=body.wait_s)
    async with db.read() as conn:
        return await workspace_cleanup.summary(conn, settings, cleaner.busy)
