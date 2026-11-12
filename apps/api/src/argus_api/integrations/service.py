"""Integrations: records, SOPS-held secrets and validation
(docs/specs/integrations.spec.md). Every transport calls these functions."""

import asyncio
import base64
import binascii
import json
import re
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import datetime
from functools import wraps
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import aiosqlite
import anyio
import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from argus_api.db import Database, Tx, ts
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.integrations import checks
from argus_api.secrets import SecretStore, SecretStoreError, ref_name

Kind = Literal["mcp", "kubernetes"]
Access = Literal["read_only", "read_write"]

STORE_ENTRY_PREFIX = "integration-"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_TOKEN = re.compile(r"^[\x21-\x7e]{1,8192}$")  # printable ASCII, no whitespace
_MAX_URL = 2048
_MAX_CA = 65536


class IntegrationRevokedError(ConflictError):
    code = "integration_revoked"


class IntegrationChangedError(ConflictError):
    code = "integration_changed"


class SecretIn(BaseModel):
    """Write-only: accepted on create and rotate, never returned."""

    model_config = ConfigDict(extra="forbid")
    token: SecretStr


class IntegrationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    name: str
    access: Access = "read_only"
    config: dict[str, Any]
    secret: SecretIn


class IntegrationRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: SecretIn


class Integration(BaseModel):
    id: str
    project: str
    kind: Kind
    name: str
    config: dict[str, Any]
    access: Access
    state: Literal["active", "revoked"]
    has_secret: bool
    validation: Literal["unchecked", "ok", "failed"]
    validation_detail: str | None
    validated_at: datetime | None
    created_at: datetime
    created_by: str
    updated_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None


def _https(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL:
        raise ValidationFailedError(
            f"{field} must be an https URL", code="config_invalid"
        )
    parts = urlsplit(value.strip())
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValidationFailedError(
            f"{field} must be an https URL without user info, query or fragment",
            code="config_invalid",
        )
    if checks.internal_address(parts.hostname):
        raise ValidationFailedError(
            f"{field} must not point at a loopback or link-local address",
            code="config_invalid",
        )
    return value.strip()


def check_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """The non-secret config, normalised; `422 config_invalid` naming the problem."""
    allowed = {"mcp": {"url"}, "kubernetes": {"server", "namespace", "ca_data"}}[kind]
    if extra := sorted(set(config) - allowed):
        raise ValidationFailedError(
            f"unknown {kind} config keys: {', '.join(extra)}", code="config_invalid"
        )
    if kind == "mcp":
        return {"url": _https(config.get("url"), "url")}
    namespace = config.get("namespace")
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        raise ValidationFailedError(
            "namespace must be a DNS-1123 label", code="config_invalid"
        )
    out: dict[str, Any] = {
        "server": _https(config.get("server"), "server"),
        "namespace": namespace,
    }
    ca = config.get("ca_data")
    if ca not in (None, ""):
        try:
            pem = base64.b64decode(str(ca), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            pem = ""
        if len(str(ca)) > _MAX_CA or "-----BEGIN CERTIFICATE-----" not in pem:
            raise ValidationFailedError(
                "ca_data must be a base64 PEM certificate bundle", code="config_invalid"
            )
        out["ca_data"] = str(ca)
    return out


def check_token(secret: SecretIn) -> str:
    token = secret.token.get_secret_value()
    if not _TOKEN.fullmatch(token):
        raise ValidationFailedError(
            "token must be 1-8192 printable characters without spaces",
            code="secret_invalid",
        )
    return token


def _model(row: aiosqlite.Row) -> Integration:
    return Integration(
        id=row["id"],
        project=row["project"],
        kind=row["kind"],
        name=row["name"],
        config=json.loads(row["config"]),
        access=row["access"],
        state=row["state"],
        has_secret=row["secret_ref"] is not None,
        validation=row["validation"],
        validation_detail=row["validation_detail"],
        validated_at=row["validated_at"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        updated_at=row["updated_at"],
        rotated_at=row["rotated_at"],
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
    )


async def _project(conn: aiosqlite.Connection, project: str) -> None:
    async with conn.execute("SELECT 1 FROM projects WHERE slug = ?", (project,)) as cur:
        if await cur.fetchone() is None:
            raise NotFoundError(f"project {project} not found")
    if project == "global":
        raise ValidationFailedError(
            "integrations belong to a project, not to global",
            code="project_not_allowed",
        )


async def _row(
    conn: aiosqlite.Connection, project: str, integration_id: str
) -> aiosqlite.Row:
    """The row only if it belongs to `project`: another project's id is a plain 404."""
    async with conn.execute(
        "SELECT * FROM integrations WHERE id = ? AND project = ?",
        (integration_id, project),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"integration {integration_id} not found in {project}")
    return row


async def _name_free(conn: aiosqlite.Connection, project: str, name: str) -> None:
    async with conn.execute(
        "SELECT 1 FROM integrations WHERE project = ? AND name = ? AND state = 'active'",
        (project, name),
    ) as cur:
        if await cur.fetchone() is not None:
            raise ConflictError(
                f"{project} already has an active integration named {name}",
                code="integration_exists",
            )


async def list_all(db: Database, project: str | None = None) -> list[Integration]:
    async with db.read() as conn:
        if project is not None:
            await _project(conn, project)
        async with conn.execute(
            "SELECT * FROM integrations WHERE ? IS NULL OR project = ?"
            " ORDER BY project, state, name, created_at",
            (project, project),
        ) as cur:
            return [_model(r) for r in await cur.fetchall()]


async def get(db: Database, project: str, integration_id: str) -> Integration:
    async with db.read() as conn:
        return _model(await _row(conn, project, integration_id))


def _mutation[**P, R](
    function: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Finish both stores and compensation before delivering caller cancellation.

    A shielded child owns the transaction, including COMMIT, SOPS subprocesses and
    filesystem threads. Never abandon that child: SQLite and rename can complete
    after their awaiting coroutine is cancelled. The AnyIO shield also handles
    request-scope cancellation; asyncio task cancellation is deferred explicitly.
    """

    @wraps(function)
    async def finish(*args: P.args, **kwargs: P.kwargs) -> R:
        with anyio.CancelScope(shield=True):
            task = asyncio.create_task(function(*args, **kwargs))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException:
                    break
            try:
                result = task.result()
            except SecretStoreError:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise SecretStoreError(
                    "integration credential store unavailable"
                ) from None
        if cancelled:
            raise asyncio.CancelledError
        return result

    return finish


def _available(available: bool) -> None:
    if not available:
        raise SecretStoreError("integration credential store unavailable")


def _credential(store: SecretStore, name: str | None) -> dict[str, Any]:
    try:
        stored = store.get(name) if name else None
    except SecretStoreError:
        stored = None
    if not stored or not isinstance(stored.get("token"), str):
        raise SecretStoreError("integration credential store unavailable")
    # Retain an independent value for rollback, never a store's mutable object.
    return dict(stored)


@_mutation
async def create(
    db: Database,
    store: SecretStore,
    project: str,
    body: IntegrationCreate,
    actor: str,
    *,
    store_available: bool = True,
) -> Integration:
    """Secret first, then the row; a failed row deletes the secret again."""
    if not _NAME.fullmatch(body.name):
        raise ValidationFailedError(
            "name must be lowercase letters, digits and dashes (max 63)",
            code="name_invalid",
        )
    config = check_config(body.kind, body.config)
    token = check_token(body.secret)
    async with db.read() as conn:
        await _project(conn, project)
        await _name_free(conn, project, body.name)
    _available(store_available)
    integration_id = str(uuid4())
    secret = f"{STORE_ENTRY_PREFIX}{integration_id}"
    try:
        await store.put(secret, {"token": token})  # may fail after applying
        async with db.write() as tx:
            await _project(tx.conn, project)
            await _name_free(tx.conn, project, body.name)
            now = ts(tx.now)
            await tx.conn.execute(
                "INSERT INTO integrations (id, project, kind, name, config, access,"
                " secret_ref, state, created_at, created_by, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    integration_id,
                    project,
                    body.kind,
                    body.name,
                    json.dumps(config),
                    body.access,
                    f"sops:{secret}",
                    now,
                    actor,
                    now,
                ),
            )
            await record(
                tx,
                actor,
                "integration",
                integration_id,
                "integration.created",
                {
                    "project": project,
                    "kind": body.kind,
                    "name": body.name,
                    "access": body.access,
                },
            )
    except BaseException:
        # Only compensate a confirmed absent row. A failed/cancelled COMMIT can
        # already have committed; a readback failure must retain the credential.
        async with db.write() as tx:
            async with tx.conn.execute(
                "SELECT 1 FROM integrations WHERE id = ?", (integration_id,)
            ) as cur:
                committed = await cur.fetchone() is not None
            if not committed:
                with suppress(SecretStoreError):
                    await store.delete(secret)
        raise
    return await get(db, project, integration_id)


@_mutation
async def _swap_secret(
    db: Database,
    store: SecretStore,
    project: str,
    integration_id: str,
    actor: str,
    revoke: bool,
    token: str | None = None,
    *,
    store_available: bool = True,
) -> Integration:
    """Rotate (`token`) or revoke inside one write; if the commit fails after the
    store changed, put the prior value back (as account delete does)."""
    secret: str | None = None
    prior: dict[str, Any] | None = None
    changed = False
    version: int | None = None
    try:
        async with db.write() as tx:
            row = await _row(tx.conn, project, integration_id)
            if row["state"] != "active":
                raise IntegrationRevokedError(f"integration {row['name']} is revoked")
            _available(store_available)
            secret = ref_name(row["secret_ref"]) if row["secret_ref"] else None
            prior = _credential(store, secret)
            assert secret is not None
            version = row["secret_version"]
            await _write_swap(tx, row, actor, revoke)
            # Set first: a store call that fails part way may still have changed it,
            # and putting the prior value back is harmless when it did not.
            changed = True
            if revoke:
                await store.delete(secret)
                if store.get(secret) is not None:
                    raise SecretStoreError(
                        "integration credential deletion unconfirmed"
                    )
            else:
                assert token is not None
                await store.put(secret, {"token": token})
    except BaseException:
        if changed and secret is not None and prior is not None:
            # Serialize readback and compensation with other mutations. Restore
            # only a confirmed rollback of this exact version, never a committed
            # revocation or rotation (nor a later mutation).
            async with db.write() as tx:
                current = await _row(tx.conn, project, integration_id)
                if (
                    current["state"] == "active"
                    and current["secret_version"] == version
                ):
                    try:
                        await store.put(secret, prior)
                    except SecretStoreError:
                        raise SecretStoreError(
                            "integration credential recovery required",
                            code="secret_restore_failed",
                        ) from None
        raise
    return await get(db, project, integration_id)


async def _write_swap(tx: Tx, row: aiosqlite.Row, actor: str, revoke: bool) -> None:
    now = ts(tx.now)
    if revoke:
        await tx.conn.execute(
            "UPDATE integrations SET state = 'revoked', secret_ref = NULL,"
            " revoked_at = ?, revoked_by = ?, updated_at = ? WHERE id = ?",
            (now, actor, now, row["id"]),
        )
    else:
        await tx.conn.execute(
            "UPDATE integrations SET secret_version = secret_version + 1,"
            " validation = 'unchecked', validation_detail = NULL, validated_at = NULL,"
            " rotated_at = ?, updated_at = ? WHERE id = ?",
            (now, now, row["id"]),
        )
    await record(
        tx,
        actor,
        "integration",
        row["id"],
        "integration.revoked" if revoke else "integration.rotated",
        {"project": row["project"], "name": row["name"]},
    )


async def rotate(
    db: Database,
    store: SecretStore,
    project: str,
    integration_id: str,
    body: IntegrationRotate,
    actor: str,
    *,
    store_available: bool = True,
) -> Integration:
    token = check_token(body.secret)
    return await _swap_secret(
        db,
        store,
        project,
        integration_id,
        actor,
        False,
        token,
        store_available=store_available,
    )


async def revoke(
    db: Database,
    store: SecretStore,
    project: str,
    integration_id: str,
    actor: str,
    *,
    store_available: bool = True,
) -> Integration:
    return await _swap_secret(
        db, store, project, integration_id, actor, True, store_available=store_available
    )


async def validate(
    db: Database,
    store: SecretStore,
    project: str,
    integration_id: str,
    actor: str,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: checks.Resolver | None = None,
    *,
    store_available: bool = True,
) -> Integration:
    """Network check outside the write; recorded only if the secret is unchanged."""
    async with db.read() as conn:
        row = await _row(conn, project, integration_id)
    if row["state"] != "active":
        raise IntegrationRevokedError(f"integration {row['name']} is revoked")
    _available(store_available)
    name = ref_name(row["secret_ref"]) if row["secret_ref"] else None
    stored = _credential(store, name)
    token = check_token(SecretIn(token=stored["token"]))
    status, detail = await checks.check(
        row["kind"],
        json.loads(row["config"]),
        row["access"],
        token,
        transport,
        resolve,
    )
    async with db.write() as tx:
        now = ts(tx.now)
        cur = await tx.conn.execute(
            "UPDATE integrations SET validation = ?, validation_detail = ?,"
            " validated_at = ?, updated_at = ?"
            " WHERE id = ? AND state = 'active' AND secret_version = ?",
            (status, detail, now, now, integration_id, row["secret_version"]),
        )
        if cur.rowcount == 0:
            raise IntegrationChangedError(
                "the integration was rotated or revoked during validation; validate again"
            )
        await record(
            tx,
            actor,
            "integration",
            integration_id,
            "integration.validated",
            {"project": project, "status": status, "detail": detail},
        )
    return await get(db, project, integration_id)
