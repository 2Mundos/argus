"""Leases: checking, renewing and disk-pausing the lease a worker holds on a task.

See docs/specs/worker-lifecycle.spec.md 3.2.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite
from argus_core.agents import LIVE
from argus_core.tasks import LeaseObservation

from argus_api.db import Snapshot, Tx, ts
from argus_api.errors import LeaseConflictError, NotFoundError
from argus_api.tasks.reads import S, _agent_state, _row

Lease = (
    tuple[str, int]
    | tuple[str, int, datetime | None]
    | tuple[str, int, datetime | None, LeaseObservation | None]
)


@dataclass(frozen=True)
class RenewedLease:
    task_id: str
    attempt: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class RejectedLease:
    task_id: str
    attempt: int
    code: str
    message: str


async def renew(
    tx: Tx,
    agent_id: str,
    leases: list[Lease],
) -> tuple[list[RenewedLease], list[RejectedLease]]:
    """Extend each still-valid lease; a rejected pair never blocks the others.

    An optional third element is `activity_at` (stall visibility): clamped to
    `<= tx.now`, never moved backwards, and clears `quiet_at` when it advances.
    An optional fourth is the host's `LeaseObservation` (3.7), stored with
    `observation_at = tx.now`; it never renews, expires or clears anything. A
    `gone` session starts or extends `session_gone_at`; any other observation, or
    a renewal without one, breaks that run (failure classes 3.2)."""
    renewed: list[RenewedLease] = []
    rejected: list[RejectedLease] = []
    for item in leases:
        task_id, attempt = item[0], item[1]
        activity_at = item[2] if len(item) > 2 else None
        observation = item[3] if len(item) > 3 else None
        try:
            row = await _owned(tx, task_id, attempt, agent_id)
        except (LeaseConflictError, NotFoundError) as exc:
            rejected.append(RejectedLease(task_id, attempt, exc.code, exc.message))
            continue
        deadline = tx.now + timedelta(seconds=row["lease_ttl_s"])
        await tx.conn.execute(
            "UPDATE tasks SET lease_expires_at = ? WHERE id = ?",
            (ts(deadline), task_id),
        )
        if activity_at is not None:
            await _record_activity(tx, task_id, row, activity_at)
        if observation is not None:
            # observation_at is this transaction's time, so it is never ahead of
            # tx.now; an older write (a skewed clock) never replaces a newer one.
            await tx.conn.execute(
                "UPDATE tasks SET observation = ?, observation_at = ?,"
                " session_gone_at = CASE WHEN ? THEN COALESCE(session_gone_at, ?)"
                " END WHERE id = ? AND (observation_at IS NULL OR observation_at <= ?)",
                (
                    observation.model_dump_json(),
                    ts(tx.now),
                    observation.session == "gone",
                    ts(tx.now),
                    task_id,
                    ts(tx.now),
                ),
            )
        else:
            await tx.conn.execute(
                "UPDATE tasks SET session_gone_at = NULL WHERE id = ?", (task_id,)
            )
        renewed.append(RenewedLease(task_id, attempt, deadline))
    return renewed, rejected


async def _record_activity(
    tx: Tx, task_id: str, row: aiosqlite.Row, activity_at: datetime
) -> None:
    """Store a newer activity signal and clear quiet when it advances."""
    at = min(activity_at, tx.now)
    previous = row["activity_at"]
    if previous is not None and at <= datetime.fromisoformat(previous):
        return
    await tx.conn.execute(
        "UPDATE tasks SET activity_at = ?, quiet_at = NULL WHERE id = ?",
        (ts(at), task_id),
    )


# -- disk guard pause (operations.spec.md 3.5) ------------------------------------
# Only the server-side disk guard sets and clears `paused_disk_at`; no route, tool,
# heartbeat or observation reaches them.

PAUSED_DISK = "paused_disk"


@dataclass(frozen=True)
class PausedLease(RenewedLease):
    reason: str = PAUSED_DISK


async def mark_paused_disk(tx: Tx, claims: list[tuple[str, int]]) -> list[str]:
    """Flag (task id, attempt) claims the guard paused; a pair that is not the
    current claimed attempt is skipped. Returns the ids newly flagged."""
    marked: list[str] = []
    for task_id, attempt in claims:
        cur = await tx.conn.execute(
            "UPDATE tasks SET paused_disk_at = ? WHERE id = ? AND attempt = ?"
            " AND state = 'claimed' AND paused_disk_at IS NULL",
            (ts(tx.now), task_id, attempt),
        )
        if cur.rowcount:
            marked.append(task_id)
    return marked


async def clear_paused_disk(
    tx: Tx, claims: list[tuple[str, int]] | None = None
) -> list[str]:
    """Unflag the given paused claims (attempt-checked), or every one with `None`.

    The pause's silence is not the session's: the quiet clock restarts at resume
    (`activity_at = tx.now`, `quiet_at` cleared), so a claim is never flagged or
    expired for time it spent paused. Returns the ids cleared."""
    where = "state = 'claimed' AND paused_disk_at IS NOT NULL"
    sets = "paused_disk_at = NULL, quiet_at = NULL, activity_at = ?"
    if claims is None:
        async with tx.conn.execute(
            f"UPDATE tasks SET {sets} WHERE {where} RETURNING id",  # noqa: S608
            (ts(tx.now),),
        ) as cur:
            return sorted(r["id"] for r in await cur.fetchall())
    cleared: list[str] = []
    for task_id, attempt in claims:
        cur = await tx.conn.execute(
            f"UPDATE tasks SET {sets} WHERE {where} AND id = ? AND attempt = ?",  # noqa: S608
            (ts(tx.now), task_id, attempt),
        )
        if cur.rowcount:
            cleared.append(task_id)
    return cleared


async def renew_paused_disk(tx: Tx) -> list[PausedLease]:
    """Extend every paused claim's still-valid lease by its `lease_ttl_s`, reason
    `paused_disk`: a paused session cannot heartbeat. A lease already past its
    deadline is not revived; `timeout_s` still applies (open question 4)."""
    async with tx.conn.execute(
        "SELECT id, attempt, lease_ttl_s FROM tasks WHERE state = 'claimed'"
        " AND paused_disk_at IS NOT NULL AND lease_expires_at > ? ORDER BY id",
        (ts(tx.now),),
    ) as cur:
        rows = await cur.fetchall()
    renewed: list[PausedLease] = []
    for row in rows:
        deadline = tx.now + timedelta(seconds=row["lease_ttl_s"])
        await tx.conn.execute(
            "UPDATE tasks SET lease_expires_at = ? WHERE id = ?",
            (ts(deadline), row["id"]),
        )
        renewed.append(PausedLease(row["id"], row["attempt"], deadline))
    return renewed


async def check_lease(
    tx: Tx | Snapshot,
    task_id: str,
    attempt: int,
    agent_id: str,
    *,
    allow_expired: bool = False,
) -> aiosqlite.Row:
    """Public ownership check for writes owned by other modules (artifacts, accounts).
    On a read `Snapshot` it is only a cheap early refusal; the write repeats it."""
    return await _owned(tx, task_id, attempt, agent_id, allow_expired=allow_expired)


async def _owned(
    tx: Tx | Snapshot,
    task_id: str,
    attempt: int,
    agent_id: str,
    *,
    allow_expired: bool = False,
) -> aiosqlite.Row:
    """Check the caller holds the current, unexpired lease (3.3), inside `tx`.

    `allow_expired` skips only the deadline check (see `fail`)."""
    state = await _agent_state(tx.conn, agent_id)
    if state not in LIVE:
        raise LeaseConflictError(
            f"agent {agent_id} is {state}; stop working", code=f"agent_{state}"
        )
    row = await _row(tx.conn, task_id)
    if row["state"] != S.CLAIMED:
        raise LeaseConflictError(
            f"task {task_id} is {row['state']}, not claimed", code="not_claimed"
        )
    if row["assignee_agent_id"] != agent_id:
        raise LeaseConflictError(
            f"task {task_id} is claimed by another agent", code="not_assignee"
        )
    if row["attempt"] != attempt:
        raise LeaseConflictError(
            f"attempt {attempt} of task {task_id} was superseded by attempt"
            f" {row['attempt']}; stop working on it",
            code="stale_attempt",
        )
    if not allow_expired and row["lease_expires_at"] <= ts(tx.now):
        raise LeaseConflictError(
            f"lease on task {task_id} expired at {row['lease_expires_at']};"
            " stop working on it",
            code="lease_expired",
        )
    return row
