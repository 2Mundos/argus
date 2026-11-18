"""Holds: parking a task for quota or a preparing runtime, and releasing it."""

import json
from datetime import datetime

import aiosqlite
from argus_core.tasks import Action, Task

from argus_api.db import Tx, ts
from argus_api.events import record
from argus_api.tasks.leases import _owned
from argus_api.tasks.manage import unblock
from argus_api.tasks.reads import _TICK, get_task
from argus_api.tasks.reasons import (
    AWAITING_QUOTA,
    RUNTIME_FAILED,
    RUNTIME_PREPARING,
    RUNTIME_PREPARING_NOTE,
    RUNTIME_SWITCH_FAILED,
)
from argus_api.tasks.transitions import _apply


async def block_switch_failed(
    tx: Tx, agent_id: str, task_id: str, attempt: int, error: str
) -> Task:
    """The old runtime did not stop, so the new account cannot be applied safely:
    block the task (`runtime_switch_failed`) with `attempt` and `failures` as they
    were. Nothing releases it automatically (`wake_waiting` only wakes quota waits):
    the task stays blocked until an operator acts."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    await _apply(
        tx,
        row,
        Action.BLOCK,
        f"agent:{agent_id}",
        set_={
            "error": error,
            "block_reason": RUNTIME_SWITCH_FAILED,
            "progress_message": "account switch failed; the old runtime did not stop",
            "progress_percent": None,
            "progress_at": ts(tx.now),
        },
        data={"reason": RUNTIME_SWITCH_FAILED, "error": error},
    )
    return await get_task(tx.conn, task_id)


async def pause_for_quota(
    tx: Tx, agent_id: str, task_id: str, attempt: int, note: str, *, wait: bool
) -> Task:
    """A provider quota ended the attempt (not the task's fault): back to `ready`
    (or, with `wait`, `blocked` as `awaiting_quota` because no account is left)
    with `failures` and `attempt` untouched, so `max_attempts` is not consumed.
    Fenced like `fail`, which it replaces for this case."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    outcome = "waiting for quota" if wait else "requeued"
    await _apply(
        tx,
        row,
        Action.BLOCK if wait else Action.REQUEUE,
        f"agent:{agent_id}",
        set_={
            "progress_message": f"attempt {attempt} paused: provider quota reached;"
            f" {outcome}",
            "progress_percent": None,
            "progress_at": ts(tx.now),
            **({"block_reason": AWAITING_QUOTA} if wait else {}),
        },
        data={"reason": "quota_exhausted", "error": note[:500]},
    )
    return await get_task(tx.conn, task_id)


async def hold_for_runtime(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    reason: str,
    error: str | None,
    *,
    void_claim: bool = False,
) -> Task:
    """The project's image is not usable for this claim: block the task before any
    model work, `attempt` and `failures` as they were. `void_claim` (the prepare
    step, before any executor started) also hands the claim's attempt number back,
    so the deferral spends nothing and the task starts with one attempt once the
    image is ready. `runtime_preparing` is woken
    by the build; `runtime_failed` carries the durable `error` and waits for a
    changed recipe or an operator rebuild."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    note = (
        RUNTIME_PREPARING_NOTE
        if reason == RUNTIME_PREPARING
        else f"project runtime unavailable: {error}"
    )
    await _apply(
        tx,
        row,
        Action.BLOCK,
        f"agent:{agent_id}",
        set_={
            "block_reason": reason,
            **({"error": error} if error else {}),
            "progress_message": note[:500],
            "progress_percent": None,
            "progress_at": ts(tx.now),
        },
        data={"reason": reason, **({"error": error} if error else {})},
        voided=void_claim,
    )
    return await get_task(tx.conn, task_id)


async def runtime_holds(
    tx: Tx, project: str, reasons: tuple[str, ...]
) -> list[aiosqlite.Row]:
    marks = ", ".join("?" for _ in reasons)
    async with tx.conn.execute(
        "SELECT * FROM tasks WHERE state = 'blocked' AND project = ?"  # noqa: S608 - placeholders only
        f" AND block_reason IN ({marks}) ORDER BY priority DESC, created_at, id",
        (project, *reasons),
    ) as cur:
        return list(await cur.fetchall())


async def release_runtime_holds(
    tx: Tx, project: str, reasons: tuple[str, ...], actor: str = "system"
) -> list[str]:
    """Return the project's runtime-held tasks to `ready`; the claim gate decides
    again. Nothing is claimed here, so no storm."""
    rows = await runtime_holds(tx, project, reasons)
    for r in rows:
        await unblock(tx, r["id"], actor)
        if r["block_reason"] == RUNTIME_PREPARING:
            await clear_runtime_note(tx, r["id"])
    return [r["id"] for r in rows]


async def clear_runtime_note(tx: Tx, task_id: str) -> None:
    """Drop the runtime-preparing progress note (and the progress it came with), and
    only that note: a message a worker wrote is left as it is."""
    await tx.conn.execute(
        "UPDATE tasks SET progress_message = NULL, progress_percent = NULL,"
        " progress_kind = NULL, progress_data = NULL, progress_at = NULL"
        " WHERE id = ? AND progress_message = ?",
        (task_id, RUNTIME_PREPARING_NOTE),
    )


async def fail_runtime_holds(tx: Tx, project: str, error: str) -> list[str]:
    """The preparation the tasks waited for failed: keep them blocked, now durably
    and with the reason (no state change, so no attempt or failure is spent)."""
    rows = await runtime_holds(tx, project, (RUNTIME_PREPARING,))
    for r in rows:
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = ?, error = ?, progress_message = ?,"
            " updated_at = ? WHERE id = ?",
            (
                RUNTIME_FAILED,
                error,
                "project runtime unavailable",
                ts(max(tx.now, datetime.fromisoformat(r["updated_at"]) + _TICK)),
                r["id"],
            ),
        )
        await record(
            tx, "system", "task", r["id"], "task.runtime_failed", {"error": error[:500]}
        )
    return [r["id"] for r in rows]


async def await_quota_ready(
    tx: Tx, agent_id: str, capabilities: list[str]
) -> list[str]:
    """Block the `ready` tasks this agent could claim as `awaiting_quota`: it has no
    usable account, so claiming would only fail. `attempt` and `failures` stay;
    `release_waiting_tasks` returns them to ready. Returns the task ids."""
    async with tx.conn.execute(
        "SELECT * FROM tasks t WHERE state = 'ready' AND NOT EXISTS ("
        "  SELECT 1 FROM json_each(t.required_capabilities) rc"
        "  WHERE rc.value NOT IN (SELECT value FROM json_each(?))) ORDER BY id",
        (json.dumps(capabilities),),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await _apply(
            tx,
            row,
            Action.BLOCK,
            f"agent:{agent_id}",
            set_={"block_reason": AWAITING_QUOTA},
            data={"reason": AWAITING_QUOTA},
        )
    return [r["id"] for r in rows]
