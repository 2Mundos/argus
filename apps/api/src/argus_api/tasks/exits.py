"""Exit classification: the automatic resume rule and the agent OOM streak.

See docs/specs/failure-classes.spec.md.
"""

from datetime import datetime
from typing import Any, NamedTuple

import aiosqlite
from argus_core.tasks import LeaseObservation
from pydantic import ValidationError

from argus_api.db import Tx
from argus_api.events import record

# Exits that end the session itself; a confirmed one follows the automatic resume
# rule (failure classes 3.2). `session_lost` is confirmed by the channel's 90 s
# listing check, `agent_ended` only by `gone` observations of the attempt that held
# without a break for as long (`SESSION_GONE_STABLE_S`).
AUTO_RESUME_REASONS = frozenset({"session_lost", "agent_ended"})
# The server's cap, in UTF-8 bytes, on an exit's `error` (the channel sends at most 2 KiB).
EXIT_ERROR_MAX = 2048
# The channel's session grace (worker lifecycle 3.8): how long `gone` must hold.
SESSION_GONE_STABLE_S = 90
# Consecutive OOM-killed sessions on one pool that block as `env_oom` (failure
# classes 3.2.1). Owner policy.
OOM_PROMOTE_STREAK = 2


_AGENT_POOL_SQL = (
    "SELECT i.pool FROM agents a JOIN instances i ON i.id = a.instance_id"
    " WHERE a.id = ?"
)


async def _oom_outcome(
    tx: Tx, agent_id: str | None, *, oom_kill: bool
) -> tuple[str, int] | None:
    """Count an attempt's end on its agent's pool (failure classes 3.2.1): an
    OOM-killed session adds one to the streak, any other outcome resets it.
    Returns the pool and its streak, or None for an agent outside any pool."""
    if agent_id is None:
        return None
    async with tx.conn.execute(_AGENT_POOL_SQL, (agent_id,)) as cur:
        found = await cur.fetchone()
    if found is None or found[0] is None:
        return None
    async with tx.conn.execute(
        "UPDATE pools SET oom_streak = CASE WHEN ? THEN oom_streak + 1 ELSE 0 END"
        " WHERE name = ? RETURNING oom_streak",
        (int(oom_kill), found[0]),
    ) as cur:
        streak = await cur.fetchone()
    return None if streak is None else (found[0], streak[0])


async def reset_oom_streak(tx: Tx, pool: str) -> None:
    """The pool's sessions get a fresh count (an unblock, a memory change)."""
    await tx.conn.execute(
        "UPDATE pools SET oom_streak = 0 WHERE name = ? AND oom_streak > 0", (pool,)
    )


class _Resume(NamedTuple):
    """The automatic resume rule's verdict on one exit (failure classes 3.2)."""

    resumed: bool  # requeued under the rule: record `task.auto_resumed`
    exhausted: bool  # no progress since the last resume: failed whatever is left
    data: dict[str, Any]  # added to the fail / expire event


_NOT_CONFIRMED = _Resume(resumed=False, exhausted=False, data={})


def _exit_confirmed(row: aiosqlite.Row) -> bool:
    """`agent_ended` is a confirmed exit only when the host's observations of this
    attempt, after its claim, saw the session gone without a break for the
    channel's grace, up to its last one (3.2, 3.7): one `gone` listing between a
    restart and a crash is not a confirmation. The last observation is not held to
    the staleness window: the agent ended because it went silent."""
    observation, at, gone_at, claimed_at = (
        row["observation"],
        row["observation_at"],
        row["session_gone_at"],
        row["claimed_at"],
    )
    if observation is None or at is None or gone_at is None or claimed_at is None:
        return False
    last, since = datetime.fromisoformat(at), datetime.fromisoformat(gone_at)
    if since < datetime.fromisoformat(claimed_at):
        return False
    if (last - since).total_seconds() < SESSION_GONE_STABLE_S:
        return False
    try:
        return LeaseObservation.model_validate_json(observation).session == "gone"
    except ValidationError:
        return False


async def _auto_resume(tx: Tx, row: aiosqlite.Row, *, retries: bool) -> _Resume:
    """Apply the automatic resume rule to a confirmed exit of the claimed attempt.

    Every read is one seek on `events_task_kind` (migration 0058); the epoch is
    the latest `task.retried` or non-`system` `task.unblock`."""
    task_id = row["id"]
    epoch = max(
        await _latest_event(tx, task_id, "task.retried"),
        await _latest_event(tx, task_id, "task.unblock", operator=True),
    )
    resumed_at = await _latest_event(tx, task_id, "task.auto_resumed", after=epoch)
    if not resumed_at or await _progressed(tx, row, after=resumed_at):
        return _Resume(resumed=retries, exhausted=False, data={})
    return _Resume(resumed=False, exhausted=True, data={"auto_resume": "no_progress"})


# The automatic resume rule's reads; each is one seek on `events_task_kind`
# (migration 0058), checked by EXPLAIN QUERY PLAN in test_auto_resume.py.
LATEST_TASK_EVENT_SQL = (
    "SELECT id FROM events WHERE subject_type = 'task' AND subject_id = ?"
    " AND kind = ? AND id > ? AND (? = 0 OR actor <> 'system')"
    " ORDER BY id DESC LIMIT 1"
)
ATTEMPT_PROGRESS_SQL = (
    "SELECT 1 FROM events WHERE subject_type = 'task' AND subject_id = ?"
    " AND kind = 'task.progress' AND id > ?"
    " AND json_extract(data, '$.attempt') = ? LIMIT 1"
)


async def _latest_event(
    tx: Tx, task_id: str, kind: str, *, after: int = 0, operator: bool = False
) -> int:
    """Id of the task's newest `kind` event after `after`, or 0. `operator` skips
    the ones the server recorded on its own (actor `system`)."""
    async with tx.conn.execute(
        LATEST_TASK_EVENT_SQL, (task_id, kind, after, int(operator))
    ) as cur:
        found = await cur.fetchone()
    return found[0] if found else 0


async def _progressed(tx: Tx, row: aiosqlite.Row, *, after: int) -> bool:
    """Whether the claimed attempt recorded a `task.progress` (a milestone is one)
    after event `after`. Attempt numbers only grow within an epoch and a late
    write of an earlier attempt is refused, so the attempt alone places it."""
    async with tx.conn.execute(
        ATTEMPT_PROGRESS_SQL, (row["id"], after, row["attempt"])
    ) as cur:
        return await cur.fetchone() is not None


async def _record_auto_resumed(
    tx: Tx, row: aiosqlite.Row, actor: str, reason: str
) -> None:
    await record(
        tx,
        actor,
        "task",
        row["id"],
        "task.auto_resumed",
        {"attempt": row["attempt"], "reason": reason},
    )
