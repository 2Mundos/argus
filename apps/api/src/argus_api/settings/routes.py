"""REST routes for typed settings (docs/specs/settings.spec.md 3.3)."""

from typing import Any

from argus_core.settings import (
    SettingDefinition,
    SettingsResponse,
    SettingValue,
    SettingWrite,
)
from fastapi import APIRouter

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
            conn, settings.model_policy_path, service.GLOBAL
        )


@router.get("/projects/{slug}/settings")
async def get_project_settings(
    slug: str, db: DbDep, settings: SettingsDep, _: CallerDep
) -> SettingsResponse:
    """Effective values for a project, with `source` per key."""
    async with db.read() as conn:
        return await service.list_settings(conn, settings.model_policy_path, slug)


@router.get("/projects/{slug}/settings/{key}/override")
async def get_project_override(
    slug: str, key: str, db: DbDep, _: CallerDep
) -> dict[str, Any]:
    """The stored project override alone (null value means inherit). Used by the
    Settings tab when editing a partial `models` override."""
    async with db.read() as conn:
        await service.ensure_project_exists(conn, slug)
        return {"key": key, "value": await service.project_override(conn, slug, key)}


@router.put("/settings/{key}")
async def put_global_setting(
    key: str,
    body: SettingWrite,
    db: DbDep,
    settings: SettingsDep,
    caller: AdminDep,
) -> SettingValue:
    async with db.write() as tx:
        return await service.put_setting(
            tx,
            settings.model_policy_path,
            service.GLOBAL,
            key,
            body.value,
            caller.actor,
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
    key: str, db: DbDep, settings: SettingsDep, caller: AdminDep
) -> SettingValue:
    """Remove the global row (or the model-policy file) so the default applies."""
    async with db.write() as tx:
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
