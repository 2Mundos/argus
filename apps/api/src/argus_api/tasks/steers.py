"""Operator steers for the attempt a worker holds (task steering spec)."""

import json
from datetime import datetime
from typing import Literal
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field, field_validator

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, NotFoundError, RateLimitedError
from argus_api.events import record

MAX_MESSAGE = 4000
MAX_PENDING = 10
MIN_INTERVAL_S = 1
PREVIEW_CHARS = 200

SteerState = Literal["pending", "delivered", "cancelled", "expired"]


class SteerCreate(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= MAX_MESSAGE:
            raise ValueError(f"message must be 1 to {MAX_MESSAGE} characters")
        return value


class Steer(BaseModel):
    id: str
    attempt: int
    message: str
    actor: str
    state: SteerState
    created_at: datetime
    delivered_at: datetime | None = None
    closed_at: datetime | None = None


class SteerNotice(BaseModel):
    """What a heartbeat hands the assignee: the text, not the lifecycle."""

    id: str
    task_id: str
    attempt: int
    message: str
    actor: str
    created_at: datetime


class SteerPage(BaseModel):
    attempt: int
    items: list[Steer] = Field(default_factory=list)


def accepts(runtime_json: str | None) -> bool:
    """True when the agent's last heartbeat reported the `steer` feature."""
    if not runtime_json:
        return False
    try:
        data = json.loads(runtime_json)
    except json.JSONDecodeError:
        return False
    features = data.get("features") if isinstance(data, dict) else None
    return isinstance(features, list) and "steer" in features


async def create(tx: Tx, task_id: str, message: str, actor: str) -> Steer:
    """Bind a steer to the claimed attempt. One write, no network."""
    row = await _task(tx.conn, task_id)
    if row["state"] != "claimed" or not row["assignee_agent_id"]:
        raise ConflictError(f"task is {row['state']}, not claimed", code="not_claimed")
    async with tx.conn.execute(
        "SELECT runtime FROM agents WHERE id = ?", (row["assignee_agent_id"],)
    ) as cur:
        agent = await cur.fetchone()
    if agent is None or not accepts(agent["runtime"]):
        raise ConflictError(
            "the assignee does not accept steers", code="steer_unsupported"
        )
    attempt = row["attempt"]
    async with tx.conn.execute(
        "SELECT count(*) FROM task_steers"
        " WHERE task_id = ? AND attempt = ? AND state = 'pending'",
        (task_id, attempt),
    ) as cur:
        counted = await cur.fetchone()
    assert counted is not None
    pending = counted[0]
    if pending >= MAX_PENDING:
        raise ConflictError(
            f"attempt {attempt} already has {MAX_PENDING} pending steers",
            code="too_many_pending",
        )
    async with tx.conn.execute(
        "SELECT created_at FROM task_steers WHERE task_id = ?"
        " ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ) as cur:
        last = await cur.fetchone()
    if last is not None:
        wait = (
            MIN_INTERVAL_S
            - (tx.now - datetime.fromisoformat(last["created_at"])).total_seconds()
        )
        if wait > 0:
            raise RateLimitedError(
                f"at most one steer per {MIN_INTERVAL_S:g} s; retry in {wait:.1f} s"
            )
    steer_id = str(uuid4())
    created = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO task_steers"
        " (id, task_id, attempt, agent_id, message, actor, state, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            steer_id,
            task_id,
            attempt,
            row["assignee_agent_id"],
            message,
            actor,
            created,
        ),
    )
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.steered",
        {
            "steer_id": steer_id,
            "attempt": attempt,
            "chars": len(message),
            "preview": message[:PREVIEW_CHARS],
        },
    )
    return Steer(
        id=steer_id,
        attempt=attempt,
        message=message,
        actor=actor,
        state="pending",
        created_at=tx.now,
    )


async def cancel(tx: Tx, task_id: str, steer_id: str, actor: str) -> None:
    """Drop a pending steer. Delivered text cannot be taken back."""
    await _task(tx.conn, task_id)
    row = await _steer_row(tx.conn, task_id, steer_id)
    if row["state"] != "pending":
        raise ConflictError(f"steer is {row['state']}, not pending", code="not_pending")
    await tx.conn.execute(
        "UPDATE task_steers SET state = 'cancelled', closed_at = ? WHERE id = ?",
        (ts(tx.now), steer_id),
    )
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.steer_cancelled",
        {"steer_id": steer_id, "attempt": row["attempt"]},
    )


async def list_steers(
    conn: aiosqlite.Connection, task_id: str, attempt: int | None
) -> SteerPage:
    row = await _task(conn, task_id)
    chosen = row["attempt"] if attempt is None else attempt
    async with conn.execute(
        "SELECT * FROM task_steers WHERE task_id = ? AND attempt = ?"
        " ORDER BY created_at, id",
        (task_id, chosen),
    ) as cur:
        rows = await cur.fetchall()
    return SteerPage(attempt=chosen, items=[_steer(r) for r in rows])


async def acknowledge(tx: Tx, agent_id: str, steer_ids: list[str]) -> None:
    """Mark injected steers delivered. Unknown or already-closed ids are ignored,
    and so is a steer for a lease this agent does not still hold."""
    seen: set[str] = set()
    for steer_id in steer_ids:
        if steer_id in seen:
            continue
        seen.add(steer_id)
        async with tx.conn.execute(
            "SELECT s.* FROM task_steers s JOIN tasks t ON t.id = s.task_id"
            " WHERE s.id = ? AND s.state = 'pending' AND s.agent_id = ?"
            " AND t.state = 'claimed' AND t.assignee_agent_id = ?"
            " AND t.attempt = s.attempt AND t.lease_expires_at > ?",
            (steer_id, agent_id, agent_id, ts(tx.now)),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            continue
        await tx.conn.execute(
            "UPDATE task_steers SET state = 'delivered', delivered_at = ? WHERE id = ?",
            (ts(tx.now), steer_id),
        )
        await record(
            tx,
            f"agent:{agent_id}",
            "task",
            row["task_id"],
            "task.steer_delivered",
            {"steer_id": steer_id, "attempt": row["attempt"]},
        )


async def pending_for(tx: Tx, leases: list[tuple[str, int]]) -> list[SteerNotice]:
    """Pending steers for leases renewed in this heartbeat, oldest first."""
    notices: list[SteerNotice] = []
    for task_id, attempt in leases:
        async with tx.conn.execute(
            "SELECT * FROM task_steers WHERE task_id = ? AND attempt = ?"
            " AND state = 'pending' ORDER BY created_at, id",
            (task_id, attempt),
        ) as cur:
            rows = await cur.fetchall()
        notices.extend(_notice(r) for r in rows)
    return notices


async def expire_attempt(tx: Tx, task_id: str, attempt: int) -> None:
    """The attempt left `claimed`. Pending steers for it are never delivered."""
    await tx.conn.execute(
        "UPDATE task_steers SET state = 'expired', closed_at = ?"
        " WHERE task_id = ? AND attempt = ? AND state = 'pending'",
        (ts(tx.now), task_id, attempt),
    )


async def expire_task(tx: Tx, task_id: str) -> None:
    """Drop every pending steer on the task (a retry starts clean)."""
    await tx.conn.execute(
        "UPDATE task_steers SET state = 'expired', closed_at = ?"
        " WHERE task_id = ? AND state = 'pending'",
        (ts(tx.now), task_id),
    )


async def _task(conn: aiosqlite.Connection, task_id: str) -> aiosqlite.Row:
    async with conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"task not found: {task_id}")
    return row


async def _steer_row(
    conn: aiosqlite.Connection, task_id: str, steer_id: str
) -> aiosqlite.Row:
    async with conn.execute(
        "SELECT * FROM task_steers WHERE id = ? AND task_id = ?",
        (steer_id, task_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"steer not found: {steer_id}")
    return row


def _steer(row: aiosqlite.Row) -> Steer:
    return Steer(
        id=row["id"],
        attempt=row["attempt"],
        message=row["message"],
        actor=row["actor"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        delivered_at=_dt(row["delivered_at"]),
        closed_at=_dt(row["closed_at"]),
    )


def _notice(row: aiosqlite.Row) -> SteerNotice:
    return SteerNotice(
        id=row["id"],
        task_id=row["task_id"],
        attempt=row["attempt"],
        message=row["message"],
        actor=row["actor"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
