"""Settings store: registry, validation, resolution (docs/specs/settings.spec.md)."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from argus_core.settings import (
    IndependentReviewSetting,
    SettingDefinition,
    SettingsResponse,
    SettingValue,
)
from jsonschema import Draft202012Validator

from argus_api import model_policy
from argus_api.db import Tx, ts
from argus_api.errors import NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.knowledge import service as knowledge
from argus_api.settings import registry

GLOBAL = "global"
MODELS_KEY = "models"
REVIEW_KEY = "review.independent"


def _check_schema(defn: SettingDefinition, value: Any) -> None:
    errors = sorted(
        Draft202012Validator(defn.schema_).iter_errors(value),
        key=lambda e: list(e.path),
    )
    if errors:
        raise ValidationFailedError(
            f"invalid value for {defn.key}",
            details=[
                f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors
            ],
        )


def _normalize(key: str, value: Any) -> Any:
    """Validate and normalise a write. `models` uses the model-policy rules."""
    defn = registry.definition(key)
    if defn is None:
        raise ValidationFailedError(f"unknown setting key: {key}", code="unknown_key")
    if key == MODELS_KEY:
        # A full policy (global) needs both required maps; a project override is partial.
        return value  # caller picks validate vs validate_partial
    _check_schema(defn, value)
    if key == REVIEW_KEY:
        return IndependentReviewSetting.model_validate(value).model_dump()
    return value


async def _row(
    conn: aiosqlite.Connection, scope: str, key: str
) -> aiosqlite.Row | None:
    async with conn.execute(
        "SELECT scope, key, value, updated_at, updated_by FROM settings"
        " WHERE scope = ? AND key = ?",
        (scope, key),
    ) as cur:
        return await cur.fetchone()


async def _require_project(conn: aiosqlite.Connection, slug: str) -> None:
    if slug == GLOBAL:
        return
    await knowledge.get_project(conn, slug)  # raises unknown_project


def _models_from_file(path: Path) -> SettingValue:
    """The effective global model policy as a setting (settings.spec.md 3.4)."""
    policy = model_policy._read(path)
    plain: model_policy.Policy = {
        "harness": policy.harness,
        "implementation": policy.implementation,
    }
    for scope in model_policy.OPTIONAL_SCOPES:
        if (chains := getattr(policy, scope)) is not None:
            plain[scope] = chains
    # Map model_policy sources onto setting sources: file → global.
    source = {"env": "env", "file": "global", "defaults": "default"}[policy.source]
    return SettingValue(
        key=MODELS_KEY,
        value=plain,
        source=source,  # type: ignore[arg-type]
        revision=policy.revision,
        notice=policy.notice,
    )


async def _models_for_project(
    conn: aiosqlite.Connection, path: Path, project: str
) -> SettingValue:
    """Effective (merged) policy for a project, with source of the winning layer."""
    base = _models_from_file(path)
    if base.source == "env":
        # ARGUS_MODEL_POLICY wins over project overrides (settings.spec.md 3.4).
        return base
    override_row = await _row(conn, project, MODELS_KEY)
    if override_row is None:
        return base
    override = model_policy.validate_partial(json.loads(override_row["value"]))
    merged = model_policy.merge_policy(base.value, override)
    return SettingValue(
        key=MODELS_KEY,
        value=merged,
        source="project",
        revision=model_policy.revision(merged),
        updated_at=datetime.fromisoformat(override_row["updated_at"]),
        updated_by=override_row["updated_by"],
    )


async def _setting(
    conn: aiosqlite.Connection,
    path: Path,
    scope: str,
    key: str,
) -> SettingValue:
    defn = registry.definition(key)
    if defn is None:
        raise ValidationFailedError(f"unknown setting key: {key}", code="unknown_key")
    if key == MODELS_KEY:
        if scope == GLOBAL:
            return _models_from_file(path)
        return await _models_for_project(conn, path, scope)

    project_row = None if scope == GLOBAL else await _row(conn, scope, key)
    if project_row is not None:
        return SettingValue(
            key=key,
            value=json.loads(project_row["value"]),
            source="project",
            updated_at=datetime.fromisoformat(project_row["updated_at"]),
            updated_by=project_row["updated_by"],
        )
    global_row = await _row(conn, GLOBAL, key)
    if global_row is not None:
        return SettingValue(
            key=key,
            value=json.loads(global_row["value"]),
            source="global",
            updated_at=datetime.fromisoformat(global_row["updated_at"]),
            updated_by=global_row["updated_by"],
        )
    return SettingValue(key=key, value=defn.default, source="default")


async def list_settings(
    conn: aiosqlite.Connection, path: Path, scope: str
) -> SettingsResponse:
    await _require_project(conn, scope)
    values = [
        await _setting(conn, path, scope, defn.key)
        for defn in registry.all_definitions()
    ]
    return SettingsResponse(scope=scope, settings=values)


async def get_setting(
    conn: aiosqlite.Connection, path: Path, scope: str, key: str
) -> SettingValue:
    await _require_project(conn, scope)
    return await _setting(conn, path, scope, key)


async def project_override(
    conn: aiosqlite.Connection, project: str, key: str
) -> Any | None:
    """The stored project override value, or None when inheriting. Used by the
    dashboard when editing a partial `models` override."""
    row = await _row(conn, project, key)
    return json.loads(row["value"]) if row else None


def effective_review(value: Any) -> IndependentReviewSetting:
    return IndependentReviewSetting.model_validate(value)


async def get_review(
    conn: aiosqlite.Connection, path: Path, project: str
) -> IndependentReviewSetting:
    setting = await _setting(conn, path, project, REVIEW_KEY)
    return effective_review(setting.value)


async def effective_model_policy(
    conn: aiosqlite.Connection, path: Path, project: str
) -> tuple[model_policy.Policy, str]:
    """(merged policy, revision) for a task's project at claim time."""
    setting = await _models_for_project(conn, path, project)
    policy = model_policy.validate(setting.value)
    return policy, setting.revision or model_policy.revision(policy)


async def put_setting(
    tx: Tx,
    path: Path,
    scope: str,
    key: str,
    value: Any,
    actor: str,
) -> SettingValue:
    defn = registry.definition(key)
    if defn is None:
        raise ValidationFailedError(f"unknown setting key: {key}", code="unknown_key")
    allowed = "global" if scope == GLOBAL else "project"
    if allowed not in defn.scopes:
        raise ValidationFailedError(
            f"{key} cannot be set at {allowed} scope",
            code="scope_not_allowed",
        )
    await _require_project(tx.conn, scope)

    if key == MODELS_KEY and scope == GLOBAL:
        # Global models still live in data/model-policy.json (settings.spec.md 3.4).
        policy = model_policy.validate(value)
        model_policy._env()  # fail closed on a malformed lock
        before = _models_from_file(path).value
        model_policy._write(path, policy)
        after = _models_from_file(path)
        await record(
            tx,
            actor,
            "settings",
            f"{scope}:{key}",
            "settings.changed",
            {"scope": scope, "key": key, "from": before, "to": after.value},
        )
        return after

    if key == MODELS_KEY:
        normalised = model_policy.validate_partial(value)
        _check_schema(defn, normalised)
    else:
        normalised = _normalize(key, value)

    row = await _row(tx.conn, scope, key)
    previous_value = json.loads(row["value"]) if row else None

    await tx.conn.execute(
        "INSERT INTO settings (scope, key, value, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (scope, key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (scope, key, json.dumps(normalised), ts(tx.now), actor),
    )
    await record(
        tx,
        actor,
        "settings",
        f"{scope}:{key}",
        "settings.changed",
        {"scope": scope, "key": key, "from": previous_value, "to": normalised},
    )
    return await _setting(tx.conn, path, scope, key)


async def reset_setting(
    tx: Tx, path: Path, scope: str, key: str, actor: str
) -> SettingValue:
    defn = registry.definition(key)
    if defn is None:
        raise ValidationFailedError(f"unknown setting key: {key}", code="unknown_key")
    await _require_project(tx.conn, scope)

    if key == MODELS_KEY and scope == GLOBAL:
        model_policy._env()
        before = _models_from_file(path).value
        path.unlink(missing_ok=True)  # noqa: ASYNC240 - same as model_policy.restore_models
        after = _models_from_file(path)
        await record(
            tx,
            actor,
            "settings",
            f"{scope}:{key}",
            "settings.changed",
            {"scope": scope, "key": key, "from": before, "to": after.value},
        )
        return after

    row = await _row(tx.conn, scope, key)
    if row is None:
        # Already inheriting; still return the effective value.
        return await _setting(tx.conn, path, scope, key)
    previous = json.loads(row["value"])
    await tx.conn.execute(
        "DELETE FROM settings WHERE scope = ? AND key = ?", (scope, key)
    )
    after = await _setting(tx.conn, path, scope, key)
    await record(
        tx,
        actor,
        "settings",
        f"{scope}:{key}",
        "settings.changed",
        {"scope": scope, "key": key, "from": previous, "to": after.value},
    )
    return after


async def ensure_project_exists(conn: aiosqlite.Connection, slug: str) -> None:
    """Raise NotFoundError when the project is missing (for route 404s)."""
    try:
        await _require_project(conn, slug)
    except NotFoundError:
        raise
