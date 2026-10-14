"""Pools, scaling, and instance reads (worker lifecycle 3.5)."""

import json
from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.auth import Scope
from argus_core.decisions import Audience, DecisionKind
from argus_core.fleet import Instance, InstanceAgent, Pool, PoolUpsert

from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.events import record

_CONFIG_FIELDS = ("env", "cpus", "memory_mb", "docker")


async def get_pool(conn: aiosqlite.Connection, name: str) -> Pool:
    async with conn.execute(_POOL_SELECT + " WHERE p.name = ?", (name,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"pool not found: {name}")
    return _to_pool(row)


async def list_pools(conn: aiosqlite.Connection) -> list[Pool]:
    async with conn.execute(_POOL_SELECT + " ORDER BY p.name") as cur:
        return [_to_pool(r) for r in await cur.fetchall()]


async def upsert_pool(tx: Tx, name: str, body: PoolUpsert, actor: str) -> Pool:
    if body.target != "docker" or body.runtime != "plain":
        raise ValidationFailedError(
            "only the docker target and the plain runtime are available so far",
            code="not_supported_yet",
        )
    config = json.dumps({f: getattr(body, f) for f in _CONFIG_FIELDS}, default=_dump)
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO pools (name, target, runtime, image, capabilities,"
        " agents_per_instance, desired_replicas, max_replicas, config, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (name) DO UPDATE SET target = excluded.target,"
        " runtime = excluded.runtime, image = excluded.image,"
        " capabilities = excluded.capabilities,"
        " agents_per_instance = excluded.agents_per_instance,"
        " desired_replicas = excluded.desired_replicas,"
        " max_replicas = excluded.max_replicas, config = excluded.config,"
        " updated_at = excluded.updated_at",
        (
            name,
            body.target,
            body.runtime,
            body.image,
            json.dumps(sorted(set(body.capabilities))),
            body.agents_per_instance,
            body.desired_replicas,
            body.max_replicas,
            config,
            now,
            now,
        ),
    )
    await record(
        tx, actor, "pool", name, "pool.upserted", {"desired": body.desired_replicas}
    )
    return await get_pool(tx.conn, name)


async def scale(
    tx: Tx, name: str, desired: int, actor: str, scope: Scope
) -> tuple[Pool, str | None]:
    """Set desired replicas. Up to max_replicas anyone allowed may do it; above
    it, admins raise the cap along with it, while an orchestrator's request opens a
    `policy` decision for a human instead of failing (3.5, 3.10)."""
    pool = await get_pool(tx.conn, name)
    if desired > pool.max_replicas and scope is not Scope.ADMIN:
        decision_id = await decisions.open_decision(
            tx,
            subject_type="pool",
            subject_id=name,
            kind=DecisionKind.POLICY,
            question=(
                f"{actor} asks to scale pool {name!r} to {desired} replicas, above its"
                f" cap of {pool.max_replicas}. Raise the cap and scale?"
            ),
            options=["approve", "reject"],
            audience=Audience.HUMAN,
            requested_by=actor,
            payload={"desired_replicas": desired},
        )
        return pool, decision_id
    await _set_replicas(tx, name, desired, max(desired, pool.max_replicas), actor)
    return await get_pool(tx.conn, name), None


async def apply_scale_decision(
    tx: Tx, name: str, payload: dict[str, Any], approve: bool, actor: str
) -> None:
    if not approve:
        return
    desired = int(payload["desired_replicas"])
    pool = await get_pool(tx.conn, name)
    await _set_replicas(tx, name, desired, max(desired, pool.max_replicas), actor)


async def _set_replicas(tx: Tx, name: str, desired: int, cap: int, actor: str) -> None:
    await tx.conn.execute(
        "UPDATE pools SET desired_replicas = ?, max_replicas = ?, updated_at = ?"
        " WHERE name = ?",
        (desired, cap, ts(tx.now), name),
    )
    await record(
        tx, actor, "pool", name, "pool.scaled", {"desired": desired, "max": cap}
    )


async def list_instances(
    conn: aiosqlite.Connection,
    pool: str | None = None,
    include_terminated: bool = False,
) -> list[Instance]:
    sql, params = "SELECT * FROM instances WHERE 1 = 1", []
    if pool is not None:
        sql += " AND pool = ?"
        params.append(pool)
    if not include_terminated:
        sql += " AND state != 'terminated'"
    async with conn.execute(sql + " ORDER BY created_at DESC, id", params) as cur:
        rows = await cur.fetchall()
    return [await _to_instance(conn, r) for r in rows]


async def get_instance(conn: aiosqlite.Connection, instance_id: str) -> Instance:
    async with conn.execute(
        "SELECT * FROM instances WHERE id = ?", (instance_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"instance not found: {instance_id}")
    return await _to_instance(conn, row)


async def request_termination(tx: Tx, instance_id: str, reason: str) -> None:
    """Admin kill: the reconciler stops it and confirms before freeing the slot."""
    from argus_api.fleet.reconciler import begin_terminating

    async with tx.conn.execute(
        "SELECT * FROM instances WHERE id = ?", (instance_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"instance not found: {instance_id}")
    if row["state"] in ("terminating", "terminated"):
        raise ConflictError(f"instance {instance_id} is already {row['state']}")
    await begin_terminating(tx, row["id"], reason)


_POOL_SELECT = (
    "SELECT p.*, (SELECT count(*) FROM instances i WHERE i.pool = p.name"
    " AND i.state != 'terminated') AS live_instances FROM pools p"
)


def _dump(value: Any) -> Any:
    return value.model_dump() if hasattr(value, "model_dump") else value


def _to_pool(row: aiosqlite.Row) -> Pool:
    data = dict(row)
    data["capabilities"] = json.loads(data["capabilities"])
    data |= json.loads(data.pop("config"))
    return Pool.model_validate(data)


async def _to_instance(conn: aiosqlite.Connection, row: aiosqlite.Row) -> Instance:
    async with conn.execute(
        "SELECT a.id, a.name, a.state, (SELECT count(*) FROM tasks t WHERE"
        " t.state = 'claimed' AND t.assignee_agent_id = a.id) AS claimed_tasks"
        " FROM agents a WHERE a.instance_id = ? ORDER BY a.created_at",
        (row["id"],),
    ) as cur:
        agents = [InstanceAgent.model_validate(dict(a)) for a in await cur.fetchall()]
    return Instance.model_validate(
        {**dict(row), "draining": bool(row["draining"]), "agents": agents}
    )


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
