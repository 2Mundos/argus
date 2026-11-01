"""What follows an attempt's execution (task finalization spec): when the claim
ended, the worker's checkpoint follow-up, and the derived step shown on the read."""

from datetime import datetime
from typing import Literal

import aiosqlite
from pydantic import BaseModel, Field

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, NotFoundError

# A follow-up that reported `running` and then stayed silent this long is `unknown`.
CHECKPOINT_STALE_S = 600
# Claim endings the worker itself reported: only these time the agent's execution.
WORKER_ENDS = frozenset({"complete", "fail", "block"})


class CheckpointReport(BaseModel):
    state: Literal["running", "saved", "nothing_to_save", "failed"]
    detail: str | None = Field(default=None, max_length=200)
    """The `checkpoint:` reference (saved) or the `env_checkpoint_*` reason (failed)."""


class CheckpointFacts(BaseModel):
    state: Literal["running", "saved", "nothing_to_save", "failed", "unknown"]
    started_at: datetime | None
    ended_at: datetime | None
    duration_s: int | None
    detail: str | None


class Finalization(BaseModel):
    attempt: int
    step: Literal["executing", "finalizing_checkpoint", "finished", "unknown"]
    claim_ended_at: datetime | None
    claim_end_action: str | None
    execution_s: int | None
    checkpoint: CheckpointFacts | None


async def record_claim_end(tx: Tx, task_id: str, attempt: int, action: str) -> None:
    """The claim of `attempt` ended now, by `action` (called where a task leaves
    `claimed`). Only the attempt's newest row, and only once."""
    await tx.conn.execute(
        "UPDATE task_attempts SET claim_ended_at = ?, claim_end_action = ?"
        " WHERE claim_ended_at IS NULL AND id = ("
        " SELECT max(id) FROM task_attempts WHERE task_id = ? AND attempt = ?)",
        (ts(tx.now), action, task_id, attempt),
    )


async def report_checkpoint(
    tx: Tx, agent_id: str, task_id: str, attempt: int, report: CheckpointReport
) -> None:
    """The worker's checkpoint step for an attempt whose claim has ended. Fenced by
    the attempt's own row (spec 3.3); it never writes `tasks`."""
    async with tx.conn.execute(
        "SELECT * FROM task_attempts WHERE id = ("
        " SELECT max(id) FROM task_attempts WHERE task_id = ? AND attempt = ?)",
        (task_id, attempt),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"task {task_id} has no attempt {attempt}")
    if row["agent_id"] != agent_id:
        raise ConflictError(
            f"attempt {attempt} of task {task_id} was not held by this agent",
            code="not_attempt_holder",
        )
    if row["claim_ended_at"] is None:
        raise ConflictError(
            f"attempt {attempt} of task {task_id} is still running", code="claim_open"
        )
    state = row["checkpoint_state"]
    if report.state == "running":
        if state is None:
            await tx.conn.execute(
                "UPDATE task_attempts SET checkpoint_state = 'running',"
                " checkpoint_started_at = ? WHERE id = ?",
                (ts(tx.now), row["id"]),
            )
        return
    if state == report.state:
        return
    if state != "running":
        raise ConflictError(
            "the checkpoint step is "
            + ("not started" if state is None else f"already {state}"),
            code="checkpoint_closed" if state else "checkpoint_not_started",
        )
    await tx.conn.execute(
        "UPDATE task_attempts SET checkpoint_state = ?, checkpoint_ended_at = ?,"
        " checkpoint_detail = ? WHERE id = ?",
        (report.state, ts(tx.now), report.detail, row["id"]),
    )


def _seconds(start: str | None, end: str | None) -> int | None:
    if start is None or end is None:
        return None
    return max(
        0,
        int(
            (
                datetime.fromisoformat(end) - datetime.fromisoformat(start)
            ).total_seconds()
        ),
    )


def derive(
    row: aiosqlite.Row | None, task_state: str, now: datetime
) -> Finalization | None:
    """The step of the task's newest attempt (`row`), or None when nothing is known."""
    if row is None:
        return None
    ended = row["claim_ended_at"]
    if ended is None:
        if task_state != "claimed":
            return None  # an attempt from before the migration
        return Finalization(
            attempt=row["attempt"],
            step="executing",
            claim_ended_at=None,
            claim_end_action=None,
            execution_s=None,
            checkpoint=None,
        )
    action = row["claim_end_action"]
    cp_state = row["checkpoint_state"]
    checkpoint: CheckpointFacts | None = None
    step: Literal["executing", "finalizing_checkpoint", "finished", "unknown"]
    step = "finished"
    if cp_state is not None:
        shown = cp_state
        if cp_state == "running":
            silent = (
                now - datetime.fromisoformat(row["checkpoint_started_at"])
            ).total_seconds()
            step = "unknown" if silent > CHECKPOINT_STALE_S else "finalizing_checkpoint"
            shown = "unknown" if step == "unknown" else "running"
        checkpoint = CheckpointFacts(
            state=shown,  # type: ignore[arg-type]
            started_at=row["checkpoint_started_at"],
            ended_at=row["checkpoint_ended_at"],
            duration_s=_seconds(
                row["checkpoint_started_at"], row["checkpoint_ended_at"]
            ),
            detail=row["checkpoint_detail"],
        )
    return Finalization(
        attempt=row["attempt"],
        step=step,
        claim_ended_at=ended,
        claim_end_action=action,
        execution_s=_seconds(row["claimed_at"], ended)
        if action in WORKER_ENDS
        else None,
        checkpoint=checkpoint,
    )


async def newest(
    conn: aiosqlite.Connection, task_id: str, task_state: str, now: datetime
) -> Finalization | None:
    async with conn.execute(
        "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ) as cur:
        return derive(await cur.fetchone(), task_state, now)
