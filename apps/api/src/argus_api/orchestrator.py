"""Internal orchestrator system pool and enable switch
(docs/specs/internal-orchestrator.spec.md 3.1)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.fleet import (
    DockerOptions,
    Pool,
    PoolUpsert,
    RuntimeKind,
    TargetKind,
)
from argus_core.settings import SettingValue

from argus_api.auth import Identity
from argus_api.config import Settings
from argus_api.conversations.service import TURN_TIMEOUT
from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, ForbiddenError
from argus_api.events import record
from argus_api.fleet import service as fleet
from argus_api.fleet import templates
from argus_api.fleet.targets import TargetRegistry
from argus_api.settings import service as settings_service

SYSTEM_POOL = "orchestrator"
SYSTEM_MARKER = "orchestrator"
ORCHESTRATOR_ENABLED = "orchestrator.enabled"
UPDATES_TITLE = "Updates"


async def agent_system_pool(
    conn: aiosqlite.Connection, agent_id: str | None
) -> str | None:
    """The `pools.system` value for an agent's pool, or None."""
    if agent_id is None:
        return None
    async with conn.execute(
        "SELECT p.system FROM agents a"
        " JOIN instances i ON i.id = a.instance_id"
        " JOIN pools p ON p.name = i.pool"
        " WHERE a.id = ?",
        (agent_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] else None


async def task_origin(
    conn: aiosqlite.Connection, now: datetime, agent_id: str
) -> str | None:
    """Conversation a task created by this agent belongs to (spec 3.6).

    Only the internal orchestrator has one: the conversation of its single in-flight
    turn, else Updates. With several turns in flight the origin is ambiguous, so the
    task carries none rather than a wrong one.
    """
    if await agent_system_pool(conn, agent_id) != SYSTEM_MARKER:
        return None
    async with conn.execute(
        "SELECT DISTINCT conversation_id FROM messages"
        " WHERE state = 'delivered' AND author LIKE 'admin:%' AND delivered_at > ?"
        " LIMIT 2",
        (ts(now - TURN_TIMEOUT),),
    ) as cur:
        turns = list(await cur.fetchall())
    if len(turns) > 1:
        return None
    if turns:
        return turns[0][0]
    async with conn.execute(
        "SELECT id FROM conversations WHERE kind = 'updates'"
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def require_not_internal_orchestrator(
    conn: aiosqlite.Connection, caller: Identity, *, action: str
) -> None:
    """REST-enforced fleet scaling ban for the internal orchestrator (3.5)."""
    if await agent_system_pool(conn, caller.agent_id) != SYSTEM_MARKER:
        return
    raise ForbiddenError(
        f"the internal orchestrator cannot {action}",
        code="internal_orchestrator",
    )


async def get_system_pool(conn: aiosqlite.Connection) -> Pool | None:
    async with conn.execute(
        fleet._POOL_SELECT + " WHERE p.system = ?",
        (SYSTEM_MARKER,),
    ) as cur:
        row = await cur.fetchone()
    return fleet._to_pool(row) if row else None


def _orchestrator_upsert(
    settings: Settings, *, sandboxed: bool, desired: int
) -> PoolUpsert:
    return PoolUpsert(
        runtime=RuntimeKind.NANOCLAW,
        image=settings.nanoclaw_host_image,
        capabilities=[],
        agents_per_instance=1,
        docker=DockerOptions(
            host=None,
            network=settings.nanoclaw_network,
            extra_hosts=list(templates.EXTRA_HOSTS),
            runtime=settings.nanoclaw_docker_runtime if sandboxed else None,
        ),
        nanoclaw=templates._nanoclaw_options(settings, sandboxed),
        target=TargetKind.DOCKER,
        desired_replicas=desired,
        max_replicas=1,
        env={},
        template="nanoclaw-orchestrator",
    )


async def pick_sandboxed(
    conn: aiosqlite.Connection, settings: Settings, targets: TargetRegistry
) -> bool:
    """Prefer sandboxed NanoClaw when that worker template is ready.

    Probes the node, so call it before opening the write transaction.
    """
    pools = await fleet.list_pools(conn)
    cat = await templates.catalog(settings, pools, targets)
    orch = next((t for t in cat.templates if t.id == "nanoclaw-orchestrator"), None)
    if orch is None or not orch.ready:
        missing = []
        if orch is not None:
            for readiness in orch.readiness:
                missing.extend(readiness.missing)
        raise ConflictError(
            "the nanoclaw-orchestrator template is not ready on this node",
            code="template_not_ready",
            details=missing or ["template not ready"],
        )
    sandbox = next((t for t in cat.templates if t.id == "nanoclaw-sandbox"), None)
    return bool(sandbox and sandbox.ready)


async def ensure_updates_conversation(tx: Tx, actor: str) -> str:
    async with tx.conn.execute(
        "SELECT id FROM conversations WHERE kind = 'updates'"
    ) as cur:
        row = await cur.fetchone()
    if row:
        return row[0]
    conv_id = str(uuid4())
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO conversations (id, kind, title, title_source, created_by,"
        " archived_at, session_ref, created_at, updated_at)"
        " VALUES (?, 'updates', ?, 'admin', ?, NULL, NULL, ?, ?)",
        (conv_id, UPDATES_TITLE, actor, now, now),
    )
    await record(
        tx,
        actor,
        "conversation",
        conv_id,
        "conversation.created",
        {"kind": "updates", "title": UPDATES_TITLE},
    )
    return conv_id


async def apply_enabled(
    tx: Tx,
    settings: Settings,
    enabled: bool,
    actor: str,
    *,
    sandboxed: bool = False,
) -> None:
    """Create or converge the system pool when `orchestrator.enabled` changes."""
    pools = await fleet.list_pools(tx.conn)
    existing = next((p for p in pools if p.system == SYSTEM_MARKER), None)
    if not enabled:
        if existing is not None and existing.desired_replicas != 0:
            await tx.conn.execute(
                "UPDATE pools SET desired_replicas = 0, updated_at = ? WHERE name = ?",
                (ts(tx.now), existing.name),
            )
            await record(
                tx,
                actor,
                "pool",
                existing.name,
                "pool.scaled",
                {"desired": 0, "reason": "orchestrator.disabled"},
            )
        return

    body = _orchestrator_upsert(settings, sandboxed=sandboxed, desired=1)
    if existing is None:
        await fleet.upsert_pool(tx, SYSTEM_POOL, body, actor, system=SYSTEM_MARKER)
    elif existing.desired_replicas != 1:
        await tx.conn.execute(
            "UPDATE pools SET desired_replicas = 1, updated_at = ? WHERE name = ?",
            (ts(tx.now), existing.name),
        )
        await record(
            tx,
            actor,
            "pool",
            existing.name,
            "pool.scaled",
            {"desired": 1, "reason": "orchestrator.enabled"},
        )
    await ensure_updates_conversation(tx, actor)


async def put_enabled_setting(
    tx: Tx,
    path: Path,
    settings: Settings,
    value: Any,
    actor: str,
    *,
    sandboxed: bool,
) -> SettingValue:
    """Write `orchestrator.enabled` and converge the system pool in the same tx."""
    before = await settings_service.get_setting(
        tx.conn, path, settings_service.GLOBAL, ORCHESTRATOR_ENABLED
    )
    result = await settings_service.put_setting(
        tx, path, settings_service.GLOBAL, ORCHESTRATOR_ENABLED, value, actor
    )
    if bool(before.value) != bool(result.value):
        await apply_enabled(
            tx, settings, bool(result.value), actor, sandboxed=sandboxed
        )
    return result


async def reset_enabled_setting(
    tx: Tx, path: Path, settings: Settings, actor: str
) -> SettingValue:
    """Reset `orchestrator.enabled` to its default (off) and scale the pool down."""
    result = await settings_service.reset_setting(
        tx, path, settings_service.GLOBAL, ORCHESTRATOR_ENABLED, actor
    )
    await apply_enabled(tx, settings, bool(result.value), actor)
    return result
