"""Expiry: lease, timeout and stall expiry, and quiet-claim flagging.

See docs/specs/worker-lifecycle.spec.md 3.4 and docs/specs/stall-visibility.spec.md.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import aiosqlite
from argus_core.tasks import Action, LeaseObservation, StallCause

from argus_api.db import Tx, ts
from argus_api.events import record
from argus_api.tasks.exits import (
    _NOT_CONFIRMED,
    AUTO_RESUME_REASONS,
    _auto_resume,
    _exit_confirmed,
    _oom_outcome,
    _record_auto_resumed,
)

log = logging.getLogger(__name__)


async def expire_leases(tx: Tx, failed: list[str] | None = None) -> list[str]:
    """Expire every lease past its deadline, whether or not its agent is alive.

    With `failed`, a row that cannot be expired is rolled back alone, logged and
    appended there instead of aborting the others (3.4)."""
    return await _expire_where(
        tx, "lease_expires_at <= ?", ts(tx.now), "lease_expired", failed
    )


async def expire_timeouts(tx: Tx, failed: list[str] | None = None) -> list[str]:
    """Expire every attempt past its wall-clock deadline, however healthy its
    heartbeats: a holder that is alive but never finishes must not hold a task
    forever. It counts as a failure and retries until max_attempts."""
    return await _expire_where(tx, "timeout_at <= ?", ts(tx.now), "timeout", failed)


async def expire_stalled(
    tx: Tx, stall_after_s: int, failed: list[str] | None = None
) -> list[str]:
    """Expire flagged-quiet attempts whose last signal (progress, live log,
    activity, claim) is older than `stall_after_s`, however healthy their
    heartbeats. A session that stopped working but still answers its lease would
    otherwise hold the task until the wall-clock timeout. Counts as a failure.
    0 disables; the attempt must already be flagged quiet, so the inbox warned first.
    A claim the disk guard paused is skipped: its silence is the guard's doing."""
    if stall_after_s <= 0:
        return []
    cutoff = tx.now - timedelta(seconds=stall_after_s)
    return await _expire_where(
        tx,
        "quiet_at IS NOT NULL AND paused_disk_at IS NULL AND ? IS NOT NULL",
        ts(tx.now),
        "stalled",
        failed,
        silent_before=cutoff,
    )


async def expire_agent_leases(tx: Tx, agent_id: str) -> list[str]:
    """Expire every lease an agent holds, e.g. because it was lost or left.

    All or nothing: the caller ends the agent in the same savepoint."""
    return await _expire_where(tx, "assignee_agent_id = ?", agent_id, "agent_ended")


# Reaper interval is 10 s (reaper.REAP_INTERVAL_S); three missed passes.
LATE_DETECT_AFTER_S = 30
QUIET_CLAIM_S = 1200  # default; reaper may pass Settings.quiet_claim_s
# Three heartbeat intervals (agents.service.HEARTBEAT_INTERVAL_S = 30): an older
# observation says nothing about now (3.7).
OBSERVATION_STALE_S = 90


def stall_cause(
    now: datetime,
    observation: str | None,
    observation_at: str | None,
    claimed_at: str | None = None,
    paused_disk_at: str | None = None,
) -> tuple[StallCause, str | None]:
    """Why a claim is silent, from the host's newest observation (3.7), with the
    observation time. Absent, stale (older than three heartbeats) or from before
    this attempt's claim: `unknown`. A claim the disk guard paused is `paused_disk`
    whatever the host saw: the API knows, it paused it (operations.spec.md 3.5).
    Descriptive only: no decision reads it."""
    if paused_disk_at is not None:
        return "paused_disk", None
    if observation is None or observation_at is None:
        return "unknown", None
    at = datetime.fromisoformat(observation_at)
    if (now - at).total_seconds() > OBSERVATION_STALE_S or (
        claimed_at is not None and at < datetime.fromisoformat(claimed_at)
    ):
        return "unknown", observation_at
    seen = LeaseObservation.model_validate_json(observation)
    if seen.workspace == "missing":
        return "workspace_missing", observation_at
    if seen.session == "gone":
        return "session_gone", observation_at
    if seen.delegate == "active":
        return "silent_delegate_active", observation_at
    if (seen.workspace, seen.session, seen.delegate) == ("ok", "running", "silent"):
        return "no_progress", observation_at
    return "unknown", observation_at


def _stall_cause(tx: Tx, row: aiosqlite.Row) -> tuple[StallCause, str | None]:
    return stall_cause(
        tx.now,
        row["observation"],
        row["observation_at"],
        row["claimed_at"],
        row["paused_disk_at"],
    )


_QUIET_SIGNALS = (
    "SELECT t.id, t.quiet_at, t.activity_at, t.progress_at, t.output_at, a.claimed_at,"
    " t.observation, t.observation_at, t.paused_disk_at, t.state"
    " FROM tasks t"
    " LEFT JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
)


class QuietFacts(NamedTuple):
    quiet_at: datetime | None
    last_signal_at: datetime | None
    last_signal_source: str | None
    stall_cause: StallCause | None


def quiet_facts_from_row(row: aiosqlite.Row | None, now: datetime) -> QuietFacts:
    """Quiet facts exist only while the task is claimed. A cause is given for a
    quiet claim, or for one the disk guard paused (`paused_disk`)."""
    if row is None:
        return QuietFacts(None, None, None, None)
    claimed = row["state"] == "claimed"
    quiet_at = (
        datetime.fromisoformat(row["quiet_at"])
        if claimed and row["quiet_at"] is not None
        else None
    )
    last_at: datetime | None = None
    last_source: str | None = None
    if claimed:
        last_at, last_source = last_signal(
            activity_at=row["activity_at"],
            progress_at=row["progress_at"],
            output_at=row["output_at"],
            claimed_at=row["claimed_at"],
        )
    paused_disk_at = row["paused_disk_at"] if claimed else None
    stall: StallCause | None = None
    if quiet_at is not None or paused_disk_at is not None:
        stall = stall_cause(
            now,
            row["observation"],
            row["observation_at"],
            row["claimed_at"],
            paused_disk_at,
        )[0]
    return QuietFacts(quiet_at, last_at, last_source, stall)


async def quiet_facts(
    conn: aiosqlite.Connection, task_id: str, now: datetime
) -> QuietFacts:
    async with conn.execute(f"{_QUIET_SIGNALS} WHERE t.id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return quiet_facts_from_row(row, now)


async def quiet_facts_for(
    conn: aiosqlite.Connection, task_ids: list[str], now: datetime
) -> dict[str, QuietFacts]:
    """Quiet facts for many tasks in one query; ids with no row are absent."""
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    async with conn.execute(
        f"{_QUIET_SIGNALS} WHERE t.id IN ({marks})", task_ids
    ) as cur:
        rows = await cur.fetchall()
    return {r["id"]: quiet_facts_from_row(r, now) for r in rows}


def last_signal(
    *,
    activity_at: str | None = None,
    progress_at: str | None = None,
    output_at: str | None = None,
    claimed_at: str | None = None,
) -> tuple[datetime | None, str | None]:
    """Newest of activity / progress / live log / claim time, with its source."""
    candidates: list[tuple[datetime, str]] = []
    for value, source in (
        (activity_at, "activity"),
        (progress_at, "progress"),
        (output_at, "log"),
    ):
        if value is not None:
            candidates.append((datetime.fromisoformat(value), source))
    if claimed_at is not None:
        candidates.append((datetime.fromisoformat(claimed_at), "claim"))
    if not candidates:
        return None, None
    at, source = max(candidates, key=lambda c: c[0])
    return at, source


def _last_signal(
    row: aiosqlite.Row, *, claimed_at: str | None = None
) -> tuple[datetime | None, str | None]:
    return last_signal(
        activity_at=row["activity_at"],
        progress_at=row["progress_at"],
        output_at=row["output_at"],
        claimed_at=claimed_at,
    )


def _expiry_error(
    row: aiosqlite.Row,
    reason: str,
    *,
    last_signal_at: datetime | None = None,
    last_signal_source: str | None = None,
    detected_late_s: int | None = None,
    cause: StallCause = "unknown",
) -> str:
    holder = row["assignee_agent_id"]
    match reason:
        case "lease_expired":
            base = f"lease expired at {row['lease_expires_at']} (holder {holder})"
        case "timeout":
            base = f"attempt timed out at {row['timeout_at']} (holder {holder})"
        case "stalled":
            base = f"attempt stalled: no progress, output or activity (holder {holder})"
        case _:
            base = f"holder {holder} ended; its lease was released"
    if last_signal_at is not None and last_signal_source is not None:
        base += f"; last signal {last_signal_source} at {ts(last_signal_at)}"
    if reason == "stalled" or cause != "unknown":
        base += f"; stall cause: {cause.replace('_', ' ')}"
    if detected_late_s is not None and detected_late_s > 0:
        mins = max(1, round(detected_late_s / 60))
        base += f"; detected {mins} min late (Argus was not running)"
    return base


def _detected_late_s(tx: Tx, deadline: str | None) -> int | None:
    if deadline is None:
        return None
    late = int((tx.now - datetime.fromisoformat(deadline)).total_seconds())
    return late if late > LATE_DETECT_AFTER_S else None


async def flag_quiet(
    tx: Tx, quiet_after_s: int = QUIET_CLAIM_S, failed: list[str] | None = None
) -> list[str]:
    """Flag claimed tasks with no signal for `quiet_after_s` (stall visibility).

    Sets `quiet_at` once per silence and emits `task.quiet`. Never expires. A claim
    the disk guard paused is skipped (operations.spec.md 3.5)."""
    async with tx.conn.execute(
        "SELECT t.*, a.claimed_at FROM tasks t"
        " JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
        " WHERE t.state = 'claimed' AND t.quiet_at IS NULL"
        " AND t.paused_disk_at IS NULL ORDER BY t.id"
    ) as cur:
        rows = await cur.fetchall()
    quieted: list[str] = []
    cutoff = tx.now - timedelta(seconds=quiet_after_s)
    for row in rows:
        last_at, source = _last_signal(row, claimed_at=row["claimed_at"])
        if last_at is None or last_at > cutoff:
            continue
        cause, observed_at = _stall_cause(tx, row)
        try:
            async with tx.savepoint():
                await tx.conn.execute(
                    "UPDATE tasks SET quiet_at = ? WHERE id = ? AND quiet_at IS NULL"
                    " AND paused_disk_at IS NULL",
                    (ts(tx.now), row["id"]),
                )
                await record(
                    tx,
                    "system",
                    "task",
                    row["id"],
                    "task.quiet",
                    {
                        "attempt": row["attempt"],
                        "agent_id": row["assignee_agent_id"],
                        "last_signal_at": ts(last_at),
                        "last_signal_source": source,
                        "stall_cause": cause,
                        "observation_at": observed_at,
                    },
                )
        except Exception:
            if failed is None:
                raise
            log.exception("could not flag quiet task %s", row["id"])
            failed.append(row["id"])
            continue
        quieted.append(row["id"])
    return quieted


async def _expire_where(
    tx: Tx,
    condition: str,
    param: str,
    reason: str,
    failed: list[str] | None = None,
    *,
    silent_before: datetime | None = None,
) -> list[str]:
    """Expire the claimed tasks matching `condition`; with `silent_before` only
    those whose last signal is older than it."""
    async with tx.conn.execute(
        f"SELECT t.*, a.claimed_at FROM tasks t"  # noqa: S608
        f" LEFT JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
        f" WHERE t.state = 'claimed' AND {condition} ORDER BY t.id",
        (param,),
    ) as cur:
        rows = await cur.fetchall()
    # Through the facade: tests replace `service._apply` to fail one row.
    from argus_api.tasks import service  # it imports this module

    expired: list[str] = []
    for row in rows:
        last_at, source = _last_signal(row, claimed_at=row["claimed_at"])
        if silent_before is not None and (last_at is None or last_at > silent_before):
            continue
        deadline = {
            "lease_expired": row["lease_expires_at"],
            "timeout": row["timeout_at"],
        }.get(reason)
        late = _detected_late_s(tx, deadline)
        cause, observed_at = _stall_cause(tx, row)
        error = _expiry_error(
            row,
            reason,
            last_signal_at=last_at,
            last_signal_source=source,
            detected_late_s=late,
            cause=cause,
        )
        data: dict[str, Any] = {
            "agent_id": row["assignee_agent_id"],
            "reason": reason,
            "error": error,
            "stall_cause": cause,
            "observation_at": observed_at,
        }
        if last_at is not None:
            data["last_signal_at"] = ts(last_at)
            data["last_signal_source"] = source
        if late is not None:
            data["detected_late_s"] = late
        retries = row["failures"] + 1 < row["max_attempts"]
        try:
            async with tx.savepoint():
                resume = (
                    await _auto_resume(tx, row, retries=retries)
                    if reason in AUTO_RESUME_REASONS and _exit_confirmed(row)
                    else _NOT_CONFIRMED
                )
                data |= resume.data
                await _oom_outcome(tx, row["assignee_agent_id"], oom_kill=False)
                await service._apply(
                    tx,
                    row,
                    Action.EXPIRE,
                    "system",
                    set_={
                        "failures": row["failures"] + 1,
                        "error": error,
                        "progress_message": (
                            f"attempt {row['attempt']} ended: "
                            f"{reason.replace('_', ' ')};"
                            f" {'requeued' if retries and not resume.exhausted else 'failed'}"
                        ),
                        "progress_percent": None,
                        "quiet_at": None,
                        "activity_at": None,
                        "observation": None,
                        "observation_at": None,
                        "session_gone_at": None,
                    },
                    data=data,
                    exhausted=resume.exhausted,
                )
                if resume.resumed:
                    await _record_auto_resumed(tx, row, "system", reason)
        except Exception:
            if failed is None:
                raise
            log.exception("could not expire task %s (%s)", row["id"], reason)
            failed.append(row["id"])
            continue
        expired.append(row["id"])
    return expired
