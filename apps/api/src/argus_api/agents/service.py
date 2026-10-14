"""Agent registration, heartbeats, draining, and loss (worker lifecycle 3.4)."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.agents import (
    LIVE,
    Agent,
    AgentRegister,
    AgentState,
    BootstrapTokenCreate,
)

from argus_api.accounts import service as accounts
from argus_api.agents.tokens import (
    AGENT_PREFIX,
    BOOTSTRAP_PREFIX,
    hash_token,
    new_token,
)
from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, NotFoundError, UnauthorizedError
from argus_api.events import record
from argus_api.tasks import service as tasks
from argus_api.tasks.service import RejectedLease, RenewedLease

HEARTBEAT_INTERVAL_S = 30
LOST_AFTER_S = 3 * HEARTBEAT_INTERVAL_S


# -- reads -------------------------------------------------------------------------


async def get_agent(conn: aiosqlite.Connection, agent_id: str) -> Agent:
    async with conn.execute(_SELECT + " WHERE a.id = ?", (agent_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"agent not found: {agent_id}")
    return _to_agent(row)


async def list_agents(
    conn: aiosqlite.Connection, *, state: AgentState | None = None
) -> list[Agent]:
    sql, params = _SELECT, []
    if state is not None:
        sql += " WHERE a.state = ?"
        params.append(state)
    async with conn.execute(sql + " ORDER BY a.created_at DESC, a.id", params) as cur:
        return [_to_agent(r) for r in await cur.fetchall()]


# -- registration --------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_at: datetime


async def create_bootstrap_token(
    tx: Tx, body: BootstrapTokenCreate, actor: str
) -> IssuedToken:
    token = new_token(BOOTSTRAP_PREFIX)
    expires_at = tx.now + timedelta(seconds=body.ttl_s)
    await tx.conn.execute(
        "INSERT INTO tokens (id, token_hash, kind, role, instance_id, created_by,"
        " created_at, expires_at) VALUES (?, ?, 'bootstrap', ?, ?, ?, ?, ?)",
        (
            str(uuid4()),
            hash_token(token),
            body.role,
            body.instance_id,
            actor,
            ts(tx.now),
            ts(expires_at),
        ),
    )
    return IssuedToken(token, expires_at)


@dataclass(frozen=True)
class Registration:
    agent: Agent
    token: str


async def register(tx: Tx, body: AgentRegister) -> Registration:
    """Redeem a one-time bootstrap token for a new agent identity and token."""
    async with tx.conn.execute(
        "SELECT * FROM tokens WHERE token_hash = ? AND kind = 'bootstrap'",
        (hash_token(body.bootstrap_token),),
    ) as cur:
        boot = await cur.fetchone()
    if (
        boot is None
        or boot["revoked_at"] is not None
        or boot["used_at"] is not None
        or boot["expires_at"] <= ts(tx.now)
    ):
        raise UnauthorizedError("bootstrap token is unknown, used, expired, or revoked")

    agent_id = str(uuid4())
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO agents (id, name, role, instance_id, capabilities, state,"
        " last_heartbeat_at, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            agent_id,
            body.name or agent_id[:8],
            boot["role"],
            boot["instance_id"],
            json.dumps(sorted(set(body.capabilities))),
            now,
            now,
        ),
    )
    await tx.conn.execute(
        "UPDATE tokens SET used_at = ? WHERE id = ?", (now, boot["id"])
    )
    token = new_token(AGENT_PREFIX)
    await tx.conn.execute(
        "INSERT INTO tokens (id, token_hash, kind, role, agent_id, instance_id,"
        " created_by, created_at) VALUES (?, ?, 'agent', ?, ?, ?, ?, ?)",
        (
            str(uuid4()),
            hash_token(token),
            boot["role"],
            agent_id,
            boot["instance_id"],
            f"bootstrap:{boot['id']}",
            now,
        ),
    )
    await record(
        tx,
        f"agent:{agent_id}",
        "agent",
        agent_id,
        "agent.registered",
        {"role": boot["role"], "instance_id": boot["instance_id"]},
    )
    return Registration(await get_agent(tx.conn, agent_id), token)


# -- liveness ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Heartbeat:
    state: AgentState
    renewed: list[RenewedLease]
    rejected: list[RejectedLease]


async def heartbeat(tx: Tx, agent_id: str, leases: list[tuple[str, int]]) -> Heartbeat:
    """Refresh the agent and renew the leases it lists, in one transaction."""
    state = await _live_state(tx.conn, agent_id)
    await tx.conn.execute(
        "UPDATE agents SET last_heartbeat_at = ? WHERE id = ?", (ts(tx.now), agent_id)
    )
    renewed, rejected = await tasks.renew(tx, agent_id, leases)
    return Heartbeat(state, renewed, rejected)


async def drain(tx: Tx, agent_id: str, actor: str) -> Agent:
    state = await _live_state(tx.conn, agent_id)
    if state is AgentState.ACTIVE:
        await tx.conn.execute(
            "UPDATE agents SET state = 'draining' WHERE id = ?", (agent_id,)
        )
        await record(tx, actor, "agent", agent_id, "agent.draining")
    return await get_agent(tx.conn, agent_id)


async def leave(tx: Tx, agent_id: str) -> Agent:
    """Clean exit. Any task still held is expired, as if its lease ran out."""
    await _live_state(tx.conn, agent_id)
    await _end(tx, agent_id, AgentState.GONE, "left", f"agent:{agent_id}")
    return await get_agent(tx.conn, agent_id)


async def mark_lost(tx: Tx) -> list[str]:
    """Mark live agents whose heartbeat is older than LOST_AFTER_S as lost."""
    cutoff = ts(tx.now - timedelta(seconds=LOST_AFTER_S))
    async with tx.conn.execute(
        "SELECT id FROM agents WHERE state IN ('active', 'draining')"
        " AND last_heartbeat_at < ? ORDER BY id",
        (cutoff,),
    ) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    for agent_id in ids:
        await _end(tx, agent_id, AgentState.LOST, "heartbeat timeout", "system")
    return ids


async def end_instance(tx: Tx, instance_id: str, reason: str) -> list[str]:
    """An instance is going away: its live agents are lost (tasks released,
    tokens revoked) and its unused bootstrap tokens revoked, in this transaction."""
    async with tx.conn.execute(
        "SELECT id FROM agents WHERE instance_id = ? AND state IN ('active', 'draining')"
        " ORDER BY id",
        (instance_id,),
    ) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    for agent_id in ids:
        await _end(tx, agent_id, AgentState.LOST, reason, "system")
    await tx.conn.execute(
        "UPDATE tokens SET revoked_at = ? WHERE revoked_at IS NULL AND kind = 'bootstrap'"
        " AND used_at IS NULL AND instance_id = ?",
        (ts(tx.now), instance_id),
    )
    return ids


async def _end(
    tx: Tx, agent_id: str, state: AgentState, reason: str, actor: str
) -> None:
    """End an identity: release its tasks and revoke its credentials atomically.

    Revocation covers the agent's tokens and every unused bootstrap token of its
    instance, so a lost instance can never register a replacement identity.
    """
    async with tx.conn.execute(
        "SELECT instance_id FROM agents WHERE id = ?", (agent_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    instance_id = row["instance_id"]
    now = ts(tx.now)
    await tx.conn.execute(
        "UPDATE agents SET state = ?, ended_at = ?, end_reason = ? WHERE id = ?",
        (state, now, reason, agent_id),
    )
    await tx.conn.execute(
        "UPDATE tokens SET revoked_at = ? WHERE revoked_at IS NULL AND (agent_id = ?"
        " OR (? IS NOT NULL AND kind = 'bootstrap' AND used_at IS NULL"
        " AND instance_id = ?))",
        (now, agent_id, instance_id, instance_id),
    )
    released = await tasks.expire_agent_leases(tx, agent_id)
    await accounts.release(tx, agent_id, state)
    await record(
        tx,
        actor,
        "agent",
        agent_id,
        f"agent.{state}",
        {"reason": reason, "instance_id": instance_id, "released_tasks": released},
    )


async def _live_state(conn: aiosqlite.Connection, agent_id: str) -> AgentState:
    async with conn.execute(
        "SELECT state FROM agents WHERE id = ?", (agent_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"agent not found: {agent_id}")
    state = AgentState(row["state"])
    if state not in LIVE:
        raise ConflictError(
            f"agent {agent_id} is {state}; this identity cannot be used again",
            code=f"agent_{state}",
        )
    return state


_SELECT = (
    "SELECT a.*, (SELECT count(*) FROM tasks t WHERE t.state = 'claimed'"
    " AND t.assignee_agent_id = a.id) AS claimed_tasks FROM agents a"
)


def _to_agent(row: aiosqlite.Row) -> Agent:
    data: dict[str, Any] = dict(row)
    data["capabilities"] = json.loads(data["capabilities"])
    return Agent.model_validate(data)
