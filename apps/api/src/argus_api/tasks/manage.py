"""Operator task operations: unblock, retry, cancel, approve, review and release."""

from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.auth import Scope
from argus_core.continuation import clean
from argus_core.decisions import REVIEW_OPTIONS, Audience, DecisionKind
from argus_core.task_edit import RetryOverrides, TaskChanges
from argus_core.tasks import ENV_OOM, Action, Review, Task

from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import ConflictError, ForbiddenError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import cost as task_cost
from argus_api.tasks import deliverable, steers
from argus_api.tasks.cost import BUDGET_EXHAUSTED
from argus_api.tasks.edit import _diff, checked_changes, column_sets
from argus_api.tasks.exits import reset_oom_streak
from argus_api.tasks.reads import _TICK, S, _row, get_task
from argus_api.tasks.transitions import (
    _apply,
    _deps_done,
    _escalation,
    check_acceptance_met,
)


async def unblock(tx: Tx, task_id: str, actor: str) -> Task:
    row = await _row(tx.conn, task_id)
    if (
        row["state"] == S.BLOCKED
        and row["block_reason"] == BUDGET_EXHAUSTED
        and (spent := await task_cost.exhausted_reason(tx.conn, row)) is not None
    ):
        raise ConflictError(f"task {task_id}: the {spent}", code=BUDGET_EXHAUSTED)
    await _apply(tx, row, Action.UNBLOCK, actor, set_={"block_reason": None})
    if row["block_reason"] == ENV_OOM and actor != "system":
        # The operator answered the pool's OOM block (failure classes 3.2.1).
        async with tx.conn.execute(
            "SELECT json_extract(data, '$.pool') FROM events"
            " WHERE subject_type = 'task' AND subject_id = ? AND kind = 'task.block'"
            " ORDER BY id DESC LIMIT 1",
            (task_id,),
        ) as cur:
            found = await cur.fetchone()
        if found and found[0]:
            await reset_oom_streak(tx, found[0])
    return await get_task(tx.conn, task_id)


_RETRY_ERROR_MAX = 500


async def retry(
    tx: Tx, task_id: str, actor: str, overrides: RetryOverrides | None = None
) -> Task:
    """Give a `failed` task a fresh window of attempts (`max_attempts` is kept).

    `overrides` change the route, difficulty, deliverable, priority or budget in
    the same transaction (task edit spec 3.11): they are checked with the edit
    rules before the first write, so a refused override leaves the task failed
    and a claim never sees it `ready` with the old values.

    Not a state-machine action: `failed` is terminal for the workers' transitions,
    and only an operator may leave it. `failures` is reset with `attempt` because
    `failures` is what `max_attempts` limits (3.2).
    """
    row = await _row(tx.conn, task_id)
    if row["state"] != S.FAILED:
        raise ConflictError(
            f"task is {row['state']}, not failed", code="task_not_failed"
        )
    given = overrides.changes() if overrides else TaskChanges()
    if overrides and (unknown := overrides.unknown()):
        raise ValidationFailedError(
            "these fields cannot be changed by a retry: " + ", ".join(unknown),
            code="immutable_field",
            details=unknown,
        )
    changed: dict[str, Any] = {}
    applied: dict[str, Any] = {}  # the audit of what the overrides changed
    if given.given():
        current = await get_task(tx.conn, task_id)
        changed = await checked_changes(tx.conn, row, current, given.given())
        applied = {f: _diff(f, getattr(current, f), v) for f, v in changed.items()}
    await tx.conn.execute(
        "UPDATE tasks SET state = ?, attempt = 0, failures = 0, error = NULL,"
        " error_attempt = NULL, error_at = NULL,"
        " assignee_agent_id = NULL, lease_expires_at = NULL, timeout_at = NULL,"
        " progress_message = NULL, progress_percent = NULL, progress_at = NULL,"
        " progress_kind = NULL, progress_data = NULL, output_at = NULL,"
        " block_reason = NULL, archived_at = NULL, oom_kill = 0, updated_at = ?"
        " WHERE id = ?",
        (S.READY, ts(tx.now), task_id),
    )
    if sets := column_sets(changed):
        assignments = ", ".join(f"{k} = ?" for k in sets)
        await tx.conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",  # noqa: S608 - keys are code-controlled
            (*sets.values(), task_id),
        )
    await tx.conn.execute("DELETE FROM task_log WHERE task_id = ?", (task_id,))
    await tx.conn.execute("DELETE FROM task_changes WHERE task_id = ?", (task_id,))
    await tx.conn.execute(
        "DELETE FROM task_command_blocks WHERE task_id = ?", (task_id,)
    )
    await steers.close_task(tx, task_id)
    error = row["error"] or ""
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.retried",
        {
            "from": S.FAILED,
            "to": S.READY,
            "task_id": task_id,
            "previous_attempts": row["attempt"],
            "max_attempts": row["max_attempts"],
            "previous_error": error[:_RETRY_ERROR_MAX]
            + ("…" if len(error) > _RETRY_ERROR_MAX else ""),
        }
        | ({"overrides": applied} if applied else {}),
    )
    return await get_task(tx.conn, task_id)


async def review_failed(
    tx: Tx, row: aiosqlite.Row, actor: str, event: str, data: dict[str, Any]
) -> Task:
    """Move a `failed` task to `in_review` with the normal review decision, for a
    delivery verified after the failure (task deliverable spec 3.8).

    Not a state-machine action, as `retry` is not: only a manager leaves `failed`
    this way, and the caller has checked the evidence. `attempt`, `failures`,
    `error`, the checklist and every earlier event stay as they are; the decision is
    the gate that may still accept or reject."""
    if row["state"] != S.FAILED:
        raise ConflictError(
            f"task is {row['state']}, not failed", code="task_not_failed"
        )
    bumped = ts(max(tx.now, datetime.fromisoformat(row["updated_at"]) + _TICK))
    await tx.conn.execute(
        "UPDATE tasks SET state = ?, archived_at = NULL, retry_at = NULL,"
        " updated_at = ? WHERE id = ?",
        (S.IN_REVIEW, bumped, row["id"]),
    )
    review = Review(row["review"])
    decision_id = await decisions.open_decision(
        tx,
        subject_type="task",
        subject_id=row["id"],
        kind=DecisionKind.REVIEW,
        question=f"Review the result of {row['title']!r} (attempt {row['attempt']}).",
        options=list(REVIEW_OPTIONS),
        audience=Audience.HUMAN if review is Review.HUMAN else Audience.ORCHESTRATOR,
        requested_by=actor,
    )
    await record(
        tx,
        actor,
        "task",
        row["id"],
        event,
        {
            "from": S.FAILED,
            "to": S.IN_REVIEW,
            "attempt": row["attempt"],
            "failures": row["failures"],
            "decision_id": decision_id,
            **data,
        },
    )
    return await get_task(tx.conn, row["id"])


RELEASE_REASON_MAX = 500


async def release(
    tx: Tx, task_id: str, actor: str, reason: str, *, attempt: int | None = None
) -> Task:
    """An operator takes a claimed task back from its holder: `ready` again with
    `failures` untouched, so the attempt never counts toward `max_attempts` (3.3).
    `attempt`, when given, must be the current one. The holder's account leases
    end with it unless it holds another claim; its next write gets 409."""
    from argus_api.accounts import service as accounts  # it imports this module

    row = await _row(tx.conn, task_id)
    if row["state"] != S.CLAIMED:
        raise ConflictError(
            f"task is {row['state']}, not claimed", code="task_not_claimed"
        )
    if attempt is not None and attempt != row["attempt"]:
        raise ConflictError(
            f"task {task_id} is at attempt {row['attempt']}, not {attempt}",
            code="stale_attempt",
        )
    holder = row["assignee_agent_id"]
    await release_claim(tx, row, actor, reason)
    async with tx.conn.execute(
        "SELECT 1 FROM tasks WHERE state = 'claimed' AND assignee_agent_id = ?",
        (holder,),
    ) as cur:
        if await cur.fetchone() is None:
            await accounts.release(tx, holder, "task_released")
    return await get_task(tx.conn, task_id)


async def release_agent_tasks(
    tx: Tx, agent_id: str, actor: str, reason: str
) -> list[str]:
    """Release every task an agent holds (an operator ended it: 3.4). The caller
    deals with the agent's account leases."""
    async with tx.conn.execute(
        "SELECT * FROM tasks WHERE state = 'claimed' AND assignee_agent_id = ?"
        " ORDER BY id",
        (agent_id,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await release_claim(tx, row, actor, reason)
    return [r["id"] for r in rows]


async def release_claim(tx: Tx, row: aiosqlite.Row, actor: str, reason: str) -> None:
    """Requeue a claimed row without a failure and mark its attempt `released`."""
    await _apply(
        tx,
        row,
        Action.RELEASE,
        actor,
        set_={
            "error": None,
            "error_attempt": None,
            "error_at": None,
            "progress_message": f"attempt {row['attempt']} released: {reason};"
            " requeued",
            "progress_percent": None,
            "progress_at": ts(tx.now),
        },
        data={"reason": reason, "previous_holder": row["assignee_agent_id"]},
        event="task.released",
    )
    await tx.conn.execute(
        "UPDATE task_attempts SET outcome = 'released', outcome_reason = ?"
        " WHERE id = (SELECT max(id) FROM task_attempts"
        "  WHERE task_id = ? AND attempt = ?)",
        (reason, row["id"], row["attempt"]),
    )


async def cancel(tx: Tx, task_id: str, actor: str) -> Task:
    row = await _row(tx.conn, task_id)
    data: dict[str, Any] = {"cancelled_by": actor}
    set_: dict[str, Any] = {}
    if row["state"] == S.BLOCKED and row["block_reason"]:
        # Name the cause: a cancelled task keeps its block reason, so say who
        # cancelled it and why it was blocked.
        data["block_reason"] = row["block_reason"]
        set_["progress_message"] = (
            f"cancelled by {actor} while blocked as {row['block_reason']}"[:500]
        )
    await _apply(tx, row, Action.CANCEL, actor, set_=set_, data=data)
    return await get_task(tx.conn, task_id)


async def approve(tx: Tx, task_id: str, actor: str) -> Task:
    row = await _row(tx.conn, task_id)
    await _apply(
        tx, row, Action.APPROVE, actor, deps_done=await _deps_done(tx.conn, task_id)
    )
    return await get_task(tx.conn, task_id)


async def review(
    tx: Tx,
    task_id: str,
    actor: str,
    reviewer: Scope,
    *,
    accept: bool,
    notes: str = "",
    forge: deliverable.ForgeRead | None = None,
    deviation: bool = False,
) -> Task:
    """Accept or reject a result in review. `deviation` accepts over unmet acceptance
    items with `notes` as the recorded deviation (deliverable spec 3.10)."""
    row = await _row(tx.conn, task_id)
    check_reviewer(row, reviewer)
    if accept:
        deliverable.check_accept(row, forge)
        if not deviation:
            check_acceptance_met(row)
        elif not notes.strip():
            raise ValidationFailedError(
                "accept_with_deviation needs text: which criteria are unmet, why the"
                " result is accepted anyway, and who authorized it",
                code="deviation_reason_required",
            )
        await _apply(
            tx,
            row,
            Action.ACCEPT,
            actor,
            data={"notes": notes},
            disposition=("accepted_with_deviation", clean(notes))
            if deviation
            else None,
        )
    else:
        # Rejection is not a failure: `failures` is untouched (3.2).
        spec = row["spec"]
        if notes:
            spec += f"\n\n## Review notes (attempt {row['attempt']})\n\n{notes}"
        harder = _escalation(row, "verification_failed")
        await _apply(
            tx,
            row,
            Action.REJECT,
            actor,
            set_={"spec": spec} | harder.get("set", {}),
            data={"notes": notes} | harder.get("data", {}),
        )
    return await get_task(tx.conn, task_id)


ENV_DISK_FULL = "env_disk_full"


async def shed_disk_full(tx: Tx, task_id: str, attempt: int) -> bool:
    """End the disk guard's shed attempt as `env_disk_full` without spending it:
    the task is `ready` again with `failures` untouched, like a release, and the
    holder's account leases end with it unless it holds another claim. A pair that
    is not the current claimed attempt is skipped (false)."""
    from argus_api.accounts import service as accounts  # it imports this module

    row = await _row(tx.conn, task_id)
    if row["state"] != S.CLAIMED or row["attempt"] != attempt:
        return False
    holder = row["assignee_agent_id"]
    await release_claim(tx, row, "system", ENV_DISK_FULL)
    async with tx.conn.execute(
        "SELECT 1 FROM tasks WHERE state = 'claimed' AND assignee_agent_id = ?",
        (holder,),
    ) as cur:
        if await cur.fetchone() is None:
            await accounts.release(tx, holder, "task_released")
    return True


def check_reviewer(row: aiosqlite.Row, reviewer: Scope) -> None:
    if row["review"] == Review.HUMAN and reviewer is not Scope.ADMIN:
        raise ForbiddenError("this task requires human review")
