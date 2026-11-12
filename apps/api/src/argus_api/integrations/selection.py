"""Which integrations a task opted into (docs/specs/integrations.spec.md 3.6).
Ids only; kept apart from delivery so the task service can call it."""

import aiosqlite

from argus_api.db import Tx
from argus_api.errors import ValidationFailedError

MAX_PER_TASK = 8


async def check_selection(
    conn: aiosqlite.Connection, project: str, ids: list[str]
) -> list[str]:
    """The ids a task may select: the same project, kind `kubernetes`, active.
    One `422 integration_invalid` naming every bad id; an id of another project
    reads as unknown, so selection does not reveal it exists."""
    ids = sorted(set(ids))
    if len(ids) > MAX_PER_TASK:
        raise ValidationFailedError(
            f"a task may select at most {MAX_PER_TASK} integrations",
            code="integration_invalid",
        )
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT id, kind, state FROM integrations WHERE project = ? AND id IN ({marks})",  # noqa: S608
        (project, *ids),
    ) as cur:
        found = {r["id"]: r for r in await cur.fetchall()}
    problems: list[str] = []
    for i in ids:
        row = found.get(i)
        if row is None:
            problems.append(f"integrations: {i}: not an integration of {project}")
        elif row["kind"] != "kubernetes":
            problems.append(f"integrations: {i}: only kubernetes integrations")
        elif row["state"] != "active":
            problems.append(f"integrations: {i}: revoked")
    if problems:
        raise ValidationFailedError(
            "; ".join(problems), code="integration_invalid", details=problems
        )
    return ids


async def selected(conn: aiosqlite.Connection, task_id: str) -> list[str]:
    async with conn.execute(
        "SELECT integration_id FROM task_integrations WHERE task_id = ?"
        " ORDER BY integration_id",
        (task_id,),
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def select(tx: Tx, task_id: str, project: str, ids: list[str]) -> list[str]:
    """Replace the task's selection (checked first; ids only)."""
    ids = await check_selection(tx.conn, project, ids)
    await tx.conn.execute("DELETE FROM task_integrations WHERE task_id = ?", (task_id,))
    await tx.conn.executemany(
        "INSERT INTO task_integrations (task_id, integration_id) VALUES (?, ?)",
        [(task_id, i) for i in ids],
    )
    return ids
