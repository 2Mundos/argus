"""Pools, scaling, and instance reads (worker lifecycle 3.5)."""

import json
from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.agents import AgentRuntime
from argus_core.auth import Scope
from argus_core.decisions import Audience, DecisionKind
from argus_core.fleet import (
    REDACTED,
    Fact,
    FactSource,
    InspectedInstance,
    Instance,
    InstanceAgent,
    InstanceRuntime,
    Pool,
    PoolInspection,
    PoolPatch,
    PoolUpsert,
)

from argus_api.accounts import service as accounts
from argus_api.config import Settings
from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.fleet import policy

_CONFIG_FIELDS = (
    "env",
    "cpus",
    "memory_mb",
    "allow_mutable_images",
    "docker",
    "kubernetes",
    "nanoclaw",
    "template",
)


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


def redact(pool: Pool) -> Pool:
    """The pool as every response shows it: `env` keys, never values (3.3)."""
    return pool.model_copy(update={"env": dict.fromkeys(pool.env, REDACTED)})


async def patch_pool(
    tx: Tx,
    name: str,
    body: PoolPatch,
    actor: str,
    settings: Settings | None = None,
) -> Pool:
    """Apply a partial update, guarded by the pool's version (3.3)."""
    pool = await get_pool(tx.conn, name)
    if body.expected_version != pool.version:
        raise ConflictError(
            f"pool {name!r} changed since you read it (version {pool.version});"
            " read it again and reapply your change",
            code="stale_version",
            details=[pool.version],
        )
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version", "env"})
    current = PoolUpsert.model_validate(pool.model_dump()).model_dump()
    merged = {**current, **changes}
    if body.env is not None:
        env = dict(pool.env)
        for key, value in body.env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        merged["env"] = env
    try:
        upsert = PoolUpsert.model_validate(merged)
    except ValueError as exc:  # a consistency rule across fields
        raise ValidationFailedError(str(exc), code="invalid_pool") from exc
    if settings is not None:
        policy.enforce_nanoclaw_image_roles(upsert, settings)
        policy.enforce_nanoclaw_socket_policy(upsert, settings)
    await upsert_pool(tx, name, upsert, actor)
    await record(
        tx,
        actor,
        "pool",
        name,
        "pool.patched",
        {"fields": sorted([*changes, *(["env"] if body.env is not None else [])])},
    )
    return await get_pool(tx.conn, name)


async def create_pool(tx: Tx, name: str, body: PoolUpsert, actor: str) -> Pool:
    """Insert a new pool; an existing name is a conflict, never a silent replace,
    so a retried or concurrent create cannot double the workers (worker launch 3)."""
    async with tx.conn.execute("SELECT 1 FROM pools WHERE name = ?", (name,)) as cur:
        if await cur.fetchone() is not None:
            raise ConflictError(
                f"pool {name!r} already exists; launch a worker in it instead",
                code="pool_exists",
            )
    pool = await upsert_pool(tx, name, body, actor)
    await record(
        tx,
        actor,
        "pool",
        name,
        "pool.created",
        {"template": body.template, "desired": body.desired_replicas},
    )
    return pool


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


async def launch(tx: Tx, name: str, expected_desired: int | None, actor: str) -> Pool:
    """Ask for one more instance (worker launch 3). Only desired state changes;
    the reconciler starts it. Bounded by the cap and, when given, guarded by the
    caller's view of `desired_replicas` so a repeated submit adds one worker."""
    pool = await get_pool(tx.conn, name)
    if expected_desired is not None and pool.desired_replicas != expected_desired:
        raise ConflictError(
            f"pool {name!r} now wants {pool.desired_replicas} replicas, not"
            f" {expected_desired}: someone else changed it (or this was already"
            " submitted)",
            code="stale_pool",
        )
    desired = pool.desired_replicas + 1
    if desired > pool.max_replicas:
        raise ConflictError(
            f"pool {name!r} is at its cap of {pool.max_replicas} replicas;"
            " scale_pool asks for more",
            code="pool_at_capacity",
        )
    await tx.conn.execute(
        "UPDATE pools SET desired_replicas = ?, updated_at = ? WHERE name = ?",
        (desired, ts(tx.now), name),
    )
    await record(tx, actor, "pool", name, "pool.launch_requested", {"desired": desired})
    return await get_pool(tx.conn, name)


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


async def recycle(
    tx: Tx, instance_id: str, actor: str, *, requeue: bool = False
) -> list[str]:
    """Drain an instance so the reconciler replaces it: it stops only once none
    of its agents holds a claimed task, then a new one starts (3.5). With
    `requeue` the tasks they hold are released now (no failure counted), so it
    stops on the next pass. Returns the released task ids."""
    from argus_api.agents import service as agents
    from argus_api.tasks import service as tasks

    async with tx.conn.execute(
        "SELECT state, draining FROM instances WHERE id = ?", (instance_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"instance not found: {instance_id}")
    if row["state"] != "running":
        raise ConflictError(
            f"instance {instance_id} is {row['state']}; only a running one is recycled",
            code="instance_not_running",
        )
    if not row["draining"]:  # already on its way out: a retry only requeues
        await tx.conn.execute(
            "UPDATE instances SET draining = 1 WHERE id = ?", (instance_id,)
        )
        async with tx.conn.execute(
            "SELECT id FROM agents WHERE instance_id = ? AND state = 'active'",
            (instance_id,),
        ) as cur:
            for agent in await cur.fetchall():
                await agents.drain(tx, agent["id"], actor)
        await record(
            tx,
            actor,
            "instance",
            instance_id,
            "instance.recycling",
            {"requeue": requeue},
        )
    released: list[str] = []
    if requeue:
        async with tx.conn.execute(
            "SELECT id FROM agents WHERE instance_id = ?"
            " AND state IN ('active', 'draining') ORDER BY id",
            (instance_id,),
        ) as cur:
            holders = [a["id"] for a in await cur.fetchall()]
        for agent_id in holders:
            released += await tasks.release_agent_tasks(tx, agent_id, actor, "recycle")
            await accounts.release(tx, agent_id, "task_released")
    return released


async def inspect(conn: aiosqlite.Connection, name: str) -> PoolInspection:
    """The pool and its live instances with what is proven about each one."""
    pool = await get_pool(conn, name)
    inspected = [
        InspectedInstance(**i.model_dump(), runtime=_runtime(pool, i))
        for i in await list_instances(conn, name)
    ]
    return PoolInspection(pool=redact(pool), instances=inspected)


def _runtime(pool: Pool, inst: Instance) -> InstanceRuntime:
    unknown = Fact(value=None, source=FactSource.UNKNOWN)
    active = [a for a in inst.agents if a.state == "active"]
    reporting = sorted(
        (a for a in active if a.runtime is not None),
        key=lambda a: str(a.last_heartbeat_at),
        reverse=True,
    )
    reported: AgentRuntime | None = reporting[0].runtime if reporting else None

    def said(field: str) -> Fact:
        value = getattr(reported, field) if reported is not None else None
        return Fact(value=value, source=FactSource.VALIDATED) if value else unknown

    return InstanceRuntime(
        image=Fact(value=inst.image, source=FactSource.REQUESTED)
        if inst.image
        else Fact(value=pool.image, source=FactSource.CONFIGURED),
        image_digest=Fact(value=inst.image_digest, source=FactSource.STARTED)
        if inst.image_digest
        else unknown,
        worker_version=said("worker_version"),
        capabilities=Fact(
            value=sorted({c for a in active for c in a.capabilities}),
            source=FactSource.VALIDATED,
        )
        if active
        else Fact(value=pool.capabilities, source=FactSource.CONFIGURED),
        engine=said("engine"),
        executor=said("executor"),
        ready=Fact(value=True, source=FactSource.VALIDATED) if active else unknown,
    )


async def request_termination(
    tx: Tx, instance_id: str, actor: str, *, requeue: bool = True
) -> None:
    """Admin kill: the reconciler stops it and confirms before freeing the slot.
    With `requeue` (the default) the tasks its agents hold are released, not
    failed: the operator stopped them, the work did not break (3.5)."""
    from argus_api.fleet.reconciler import begin_terminating

    async with tx.conn.execute(
        "SELECT * FROM instances WHERE id = ?", (instance_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"instance not found: {instance_id}")
    if row["state"] in ("terminating", "terminated"):
        raise ConflictError(f"instance {instance_id} is already {row['state']}")
    await begin_terminating(
        tx,
        row["id"],
        f"terminated by {actor}",
        release="operator_terminate" if requeue else None,
        actor=actor,
    )


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
    data["version"] = row["updated_at"]
    # Legacy sandboxed pools may still carry a docker_socket path; the runtime
    # never mounts it, and PoolUpsert now rejects configuring both.
    docker = data.get("docker") or {}
    nanoclaw = data.get("nanoclaw")
    if (
        isinstance(docker, dict)
        and docker.get("runtime")
        and isinstance(nanoclaw, dict)
    ):
        nanoclaw.pop("docker_socket", None)
    return Pool.model_validate(data)


async def _to_instance(conn: aiosqlite.Connection, row: aiosqlite.Row) -> Instance:
    async with conn.execute(
        "SELECT a.id, a.name, a.state, a.capabilities, a.last_heartbeat_at,"
        " a.runtime, (SELECT count(*) FROM tasks t WHERE"
        " t.state = 'claimed' AND t.assignee_agent_id = a.id) AS claimed_tasks"
        " FROM agents a WHERE a.instance_id = ? ORDER BY a.created_at",
        (row["id"],),
    ) as cur:
        agents = [
            InstanceAgent.model_validate(
                {
                    **dict(a),
                    "capabilities": json.loads(a["capabilities"]),
                    "runtime": json.loads(a["runtime"]) if a["runtime"] else None,
                }
            )
            for a in await cur.fetchall()
        ]
    return Instance.model_validate(
        {**dict(row), "draining": bool(row["draining"]), "agents": agents}
    )


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
