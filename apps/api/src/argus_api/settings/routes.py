"""REST routes for typed settings (docs/specs/settings.spec.md 3.3)."""

from typing import Any

from argus_core.settings import (
    SettingDefinition,
    SettingsResponse,
    SettingValue,
    SettingWrite,
)
from fastapi import APIRouter, Request

from argus_api import orchestrator
from argus_api.access import require_project_readable
from argus_api.deps import AdminDep, CallerDep, DbDep, SettingsDep
from argus_api.settings import registry, service

router = APIRouter(tags=["settings"])


@router.get("/settings/registry")
async def get_registry(_: CallerDep) -> list[SettingDefinition]:
    """Every known setting key, its schema, default, scopes and description."""
    return registry.all_definitions()


@router.get("/settings")
async def get_global_settings(
    db: DbDep, settings: SettingsDep, _: CallerDep
) -> SettingsResponse:
    """Global values with `source` per key (env | global | default)."""
    async with db.read() as conn:
        return await service.list_settings(
            conn,
            settings.model_policy_path,
            service.GLOBAL,
            env_difficulty_executors=settings.difficulty_executors,
        )


@router.get("/projects/{slug}/settings")
async def get_project_settings(
    slug: str, db: DbDep, settings: SettingsDep, caller: CallerDep
) -> SettingsResponse:
    """Effective values for a project, with `source` per key. Workers only read
    projects of tasks they have leased."""
    async with db.read() as conn:
        await require_project_readable(conn, caller, slug)
        return await service.list_settings(
            conn,
            settings.model_policy_path,
            slug,
            env_difficulty_executors=settings.difficulty_executors,
        )


@router.get("/projects/{slug}/settings/{key}/override")
async def get_project_override(
    slug: str, key: str, db: DbDep, caller: CallerDep
) -> dict[str, Any]:
    """The stored project override alone (null value means inherit). Used by the
    Settings tab when editing a partial `models` override."""
    async with db.read() as conn:
        await require_project_readable(conn, caller, slug)
        await service.ensure_project_exists(conn, slug)
        return {"key": key, "value": await service.project_override(conn, slug, key)}


@router.put("/settings/{key}")
async def put_global_setting(
    key: str,
    body: SettingWrite,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    caller: AdminDep,
) -> SettingValue:
    path = settings.model_policy_path
    targets = request.app.state.targets
    if key == orchestrator.ORCHESTRATOR_ENABLED:
        runtime: orchestrator.Runtime = "nanoclaw"
        sandboxed = False
        # Node probes run before the writer is taken; only turning it on resolves
        # the runtime (builtin-orchestrator 3.1).
        async with db.read() as conn:
            if body.value and not await orchestrator.is_enabled(conn, path):
                runtime, sandboxed = await orchestrator.resolve_runtime(
                    conn,
                    settings,
                    targets,
                    await orchestrator.stored_runtime(conn, path),
                )
        async with db.write() as tx:
            return await orchestrator.put_enabled_setting(
                tx,
                path,
                settings,
                body.value,
                caller.actor,
                runtime=runtime,
                sandboxed=sandboxed,
            )
    if key == orchestrator.ORCHESTRATOR_RUNTIME:
        return await _write_runtime(db, settings, targets, body.value, caller.actor)
    async with db.write() as tx:
        return await service.put_setting(
            tx,
            settings.model_policy_path,
            service.GLOBAL,
            key,
            body.value,
            caller.actor,
        )


async def _write_runtime(db, settings, targets, value: Any, actor: str) -> SettingValue:
    """`orchestrator.runtime`: resolved now when the orchestrator is enabled, so
    the pool converges in the same write; otherwise only stored (3.1). `None`
    resets it to `auto`."""
    path = settings.model_policy_path
    resolved = None
    async with db.read() as conn:
        if await orchestrator.is_enabled(conn, path):
            requested = "auto" if value is None else value
            if requested in ("auto", "builtin", "nanoclaw"):
                resolved = await orchestrator.resolve_runtime(
                    conn, settings, targets, requested
                )
    async with db.write() as tx:
        return await orchestrator.write_runtime_setting(
            tx, path, settings, value, actor, resolved=resolved
        )


@router.put("/projects/{slug}/settings/{key}")
async def put_project_setting(
    slug: str,
    key: str,
    body: SettingWrite,
    db: DbDep,
    settings: SettingsDep,
    caller: AdminDep,
) -> SettingValue:
    async with db.write() as tx:
        return await service.put_setting(
            tx, settings.model_policy_path, slug, key, body.value, caller.actor
        )


@router.delete("/settings/{key}")
async def delete_global_setting(
    key: str, request: Request, db: DbDep, settings: SettingsDep, caller: AdminDep
) -> SettingValue:
    """Remove the global row (or the model-policy file) so the default applies."""
    if key == orchestrator.ORCHESTRATOR_RUNTIME:
        return await _write_runtime(
            db, settings, request.app.state.targets, None, caller.actor
        )
    async with db.write() as tx:
        if key == orchestrator.ORCHESTRATOR_ENABLED:
            return await orchestrator.reset_enabled_setting(
                tx, settings.model_policy_path, settings, caller.actor
            )
        return await service.reset_setting(
            tx, settings.model_policy_path, service.GLOBAL, key, caller.actor
        )


@router.delete("/projects/{slug}/settings/{key}")
async def delete_project_setting(
    slug: str, key: str, db: DbDep, settings: SettingsDep, caller: AdminDep
) -> SettingValue:
    """Remove the project override so the global/default value is inherited."""
    async with db.write() as tx:
        return await service.reset_setting(
            tx, settings.model_policy_path, slug, key, caller.actor
        )
