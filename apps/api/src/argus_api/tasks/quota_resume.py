"""The native session a quota pause leaves for the next claim (quota resume spec)."""

import json
from typing import Literal

import aiosqlite
from argus_core.accounts import NativeSessionReport
from argus_core.tasks import QuotaResume
from pydantic import BaseModel, Field

from argus_api.db import Tx
from argus_api.events import record
from argus_api.tasks import service


class QuotaResumeTaken(BaseModel):
    attempt: int
    outcome: Literal["resumed", "fallback"]
    reason: str | None = Field(default=None, max_length=200)
    """Why the channel started a fresh session (`fallback` only)."""


async def record_session(
    tx: Tx, agent_id: str, task_id: str, attempt: int, report: NativeSessionReport
) -> None:
    """Keep the paused attempt's native session. Called by the quota pause only, so
    a crash, stall or `session_lost` never leaves one. The node is the caller."""
    await tx.conn.execute(
        "UPDATE task_attempts SET native_session = ? WHERE task_id = ? AND attempt = ?"
        " AND native_session IS NULL",
        (
            json.dumps({**report.model_dump(), "node": agent_id}),
            task_id,
            attempt,
        ),
    )


async def offered(
    conn: aiosqlite.Connection, task_id: str, attempt: int
) -> QuotaResume | None:
    """The session of the attempt just before `attempt`, if its quota pause kept one
    and no attempt has taken it yet."""
    async with conn.execute(
        "SELECT attempt, native_session FROM task_attempts WHERE task_id = ?"
        " AND native_session_used_by IS NULL AND id = ("
        " SELECT max(id) FROM task_attempts WHERE task_id = ? AND id < ("
        "  SELECT max(id) FROM task_attempts WHERE task_id = ? AND attempt = ?))",
        (task_id, task_id, task_id, attempt),
    ) as cur:
        row = await cur.fetchone()
    if row is None or row["native_session"] is None:
        return None
    return QuotaResume(from_attempt=row["attempt"], **json.loads(row["native_session"]))


async def take(tx: Tx, agent_id: str, task_id: str, body: QuotaResumeTaken) -> bool:
    """The channel's decision for the offered session. True only the first time: the
    caller sends its one operator message on True, so a retry never sends twice."""
    attempt = body.attempt
    await service.check_lease(tx, task_id, attempt, agent_id)
    cur = await tx.conn.execute(
        "UPDATE task_attempts SET native_session_used_by = ? WHERE task_id = ?"
        " AND native_session IS NOT NULL AND native_session_used_by IS NULL"
        " AND id = (SELECT max(id) FROM task_attempts WHERE task_id = ? AND id < ("
        "  SELECT max(id) FROM task_attempts WHERE task_id = ? AND attempt = ?))",
        (attempt, task_id, task_id, task_id, attempt),
    )
    if cur.rowcount == 0:
        return False
    await record(
        tx,
        f"agent:{agent_id}",
        "task",
        task_id,
        "task.quota_resume",
        {
            "attempt": attempt,
            "outcome": body.outcome,
            **({"reason": body.reason} if body.reason else {}),
        },
    )
    return True
