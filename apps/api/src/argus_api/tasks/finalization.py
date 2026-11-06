"""What follows an attempt's execution (task finalization spec): when the claim
ended, the worker's checkpoint follow-up, and the derived step shown on the read."""

import json
from datetime import datetime
from typing import Literal

import aiosqlite
from argus_core.agents import LOST_AFTER_S
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


OwnerStatus = Literal["live", "stale", "ended", "unknown"]


class WorkspaceFacts(BaseModel):
    """Who holds the task's current workspace and whether its work is recoverable.
    Never a path or a checkpoint reference: the API knows neither."""

    owner_agent_id: str
    owner_node_id: str | None = None
    """The node the owner last reported on heartbeat; null when it reported none."""
    owner_status: OwnerStatus = "unknown"
    """`live` while the owner is active or draining and heard from within the loss
    threshold; `stale` when it has gone silent past it; `ended` when gone or lost;
    `unknown` when there is no agent row."""
    checkpoint: Literal["available", "unavailable", "unknown"]


_CHECKPOINT_STATUS: dict[str | None, Literal["available", "unavailable"]] = {
    "saved": "available",
    "failed": "unavailable",
    "nothing_to_save": "unavailable",
}


def _owner_node_id(runtime: str | None) -> str | None:
    """The `node_id` in an agent's self-reported runtime JSON, or None."""
    if not runtime:
        return None
    try:
        value = json.loads(runtime)
    except (TypeError, ValueError):
        return None
    node = value.get("node_id") if isinstance(value, dict) else None
    return node if isinstance(node, str) and node else None


def _owner_status(
    state: str | None, heartbeat: str | None, now: datetime
) -> OwnerStatus:
    """Whether the owner is still around, from its stored state and heartbeat."""
    if state is None:
        return "unknown"
    if state in ("gone", "lost"):
        return "ended"
    if state not in ("active", "draining"):
        return "unknown"
    if heartbeat is None:
        return "stale"
    silent = (now - datetime.fromisoformat(heartbeat)).total_seconds()
    return "stale" if silent > LOST_AFTER_S else "live"


def workspace_facts(row: aiosqlite.Row | None, now: datetime) -> WorkspaceFacts | None:
    """The newest attempt's worker and checkpoint status; None with no attempt.
    `running`, no step recorded and any unrecognised state are `unknown`."""
    if row is None:
        return None
    return WorkspaceFacts(
        owner_agent_id=row["agent_id"],
        owner_node_id=_owner_node_id(row["owner_runtime"]),
        owner_status=_owner_status(
            row["owner_state"], row["owner_last_heartbeat_at"], now
        ),
        checkpoint=_CHECKPOINT_STATUS.get(row["checkpoint_state"], "unknown"),
    )


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
) -> tuple[Finalization | None, WorkspaceFacts | None]:
    async with conn.execute(
        "SELECT t.*, a.state AS owner_state,"
        " a.last_heartbeat_at AS owner_last_heartbeat_at, a.runtime AS owner_runtime"
        " FROM task_attempts t LEFT JOIN agents a ON a.id = t.agent_id"
        " WHERE t.task_id = ? ORDER BY t.id DESC LIMIT 1",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    return derive(row, task_state, now), workspace_facts(row, now)
