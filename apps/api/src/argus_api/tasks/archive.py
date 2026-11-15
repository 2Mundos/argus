"""Archiving: hiding finished tasks from the board and bringing them back.

See docs/specs/task-archive.spec.md.
"""

from datetime import timedelta
from typing import Any

import aiosqlite
from argus_core.tasks import TERMINAL, Task, TaskState

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks.reads import S, _row, get_task

_ARCHIVABLE = sorted(TERMINAL)


async def archive(tx: Tx, task_id: str, actor: str) -> Task:
    """Archive a terminal task. Archiving an archived task changes nothing."""
    row = await _row(tx.conn, task_id)
    if row["archived_at"] is None:
        _require_archivable(row)
        await _set_archived(tx, row, ts(tx.now), actor)
    return await get_task(tx.conn, task_id)


async def unarchive(tx: Tx, task_id: str, actor: str) -> Task:
    """Bring an archived task back into listings and totals. Idempotent."""
    row = await _row(tx.conn, task_id)
    if row["archived_at"] is not None:
        await _set_archived(tx, row, None, actor)
    return await get_task(tx.conn, task_id)


async def archive_matching(
    tx: Tx,
    actor: str,
    *,
    states: list[TaskState],
    project: str | None = None,
    older_than_days: int | None = None,
    dry_run: bool = False,
) -> int:
    """Archive every unarchived task in `states` (terminal only), optionally in
    one project and last changed more than `older_than_days` ago. With `dry_run`
    nothing is written; either way the count is of tasks that match now."""
    if bad := sorted(set(states) - TERMINAL):
        raise ValidationFailedError(
            f"only {', '.join(_ARCHIVABLE)} tasks can be archived, not {', '.join(bad)}",
            code="state_not_archivable",
        )
    where = ["archived_at IS NULL", f"state IN ({','.join('?' * len(states))})"]
    params: list[Any] = [*states]
    if project is not None:
        where.append("project = ?")
        params.append(project)
    if older_than_days is not None:
        where.append("updated_at < ?")
        params.append(ts(tx.now - timedelta(days=older_than_days)))
    async with tx.conn.execute(
        f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY id",  # noqa: S608
        params,
    ) as cur:
        rows = list(await cur.fetchall())
    if not dry_run:
        for row in rows:
            await _set_archived(tx, row, ts(tx.now), actor)
    return len(rows)


def _require_archivable(row: aiosqlite.Row) -> None:
    if S(row["state"]) not in TERMINAL:
        raise ConflictError(
            f"task is {row['state']}; only {', '.join(_ARCHIVABLE)} tasks can be"
            " archived",
            code="task_not_terminal",
        )


async def _set_archived(
    tx: Tx, row: aiosqlite.Row, archived_at: str | None, actor: str
) -> None:
    await tx.conn.execute(
        "UPDATE tasks SET archived_at = ?, updated_at = ? WHERE id = ?",
        (archived_at, ts(tx.now), row["id"]),
    )
    kind = "task.unarchived" if archived_at is None else "task.archived"
    await record(
        tx,
        actor,
        "task",
        row["id"],
        kind,
        {"task_id": row["id"], "state": row["state"]},
    )
