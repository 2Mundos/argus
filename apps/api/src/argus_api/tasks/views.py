"""Task lists and their projections (MCP operations 3.1, 3.2).

`summary` carries state and references (ids), never the spec or result bodies;
`detail` carries the whole task with each long free-text field cut to TEXT_CAP.
"""

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.tasks import ProgressKind, Review, Task, TaskState
from pydantic import BaseModel, Field

from argus_api.db import Snapshot
from argus_api.errors import ValidationFailedError
from argus_api.paging import (
    PageInfo,
    View,
    cap,
    clamp,
    decode_cursor,
    encode_cursor,
)
from argus_api.tasks import service

MAX_IDS = 50


class Refs(BaseModel):
    depends_on: list[str]
    decision_ids: list[str]  # open decisions on the task
    artifact_ids: list[str]  # every attempt's, oldest first


class TaskSummary(Refs):
    id: str
    parent_id: str | None
    kind: str
    title: str
    project: str
    state: TaskState
    priority: int
    review: Review
    attempt: int
    max_attempts: int
    assignee_agent_id: str | None
    lease_expires_at: datetime | None
    block_reason: str | None
    progress_message: str | None
    progress_percent: int | None
    progress_at: datetime | None
    progress_kind: ProgressKind | None = None
    progress_data: dict[str, Any] | None = None
    created_at: datetime
    archived_at: datetime | None
    version: str  # updated_at; pass it back as expected_version
    age_s: int  # since creation
    truncated: dict[str, int] = {}  # field -> full length in bytes


class TaskDetail(Task, TaskSummary):
    pass


class TaskPage(PageInfo):
    items: list[TaskSummary | TaskDetail]
    missing: list[str] = Field(default_factory=list)  # requested ids not found


# What a summary leaves out of the full task.
SUMMARY_OMITS = sorted(
    set(Task.model_fields) - set(TaskSummary.model_fields) - {"updated_at"}
)


async def list_tasks(
    snap: Snapshot,
    *,
    states: list[TaskState] | None = None,
    project: str | None = None,
    kind: str | None = None,
    assignee: str | None = None,
    archived: bool = False,
    view: View = "summary",
    limit: int = 25,
    cursor: str | None = None,
) -> TaskPage:
    """Newest first, keyset-paged on (created_at, id). Archived tasks are left out
    unless `archived` asks for exactly those."""
    limit = clamp(limit)
    filters: dict[str, Any] = {"states": sorted(states or []), "project": project}
    filters |= {"kind": kind, "assignee": assignee, "archived": archived}
    where = ["archived_at IS NOT NULL" if archived else "archived_at IS NULL"]
    params: list[Any] = []
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        params += states
    if project is not None:
        where.append("project = ?")
        params.append(project)
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if assignee is not None:
        where.append("assignee_agent_id = ?")
        params.append(assignee)
    conn = snap.conn
    async with conn.execute(
        f"SELECT count(*) FROM tasks WHERE {' AND '.join(where)}",  # noqa: S608
        params,
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    total = row[0]
    if (after := decode_cursor(cursor, filters)) is not None:
        where.append("(created_at, id) < (?, ?)")
        params += after
    async with conn.execute(
        f"SELECT * FROM tasks WHERE {' AND '.join(where)}"  # noqa: S608
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        [*params, limit + 1],
    ) as cur:
        rows = list(await cur.fetchall())
    has_more = len(rows) > limit
    rows = rows[:limit]
    page = await _page(snap, rows, view)
    if has_more:
        last = rows[-1]
        page.has_more = True
        page.next_cursor = encode_cursor([last["created_at"], last["id"]], filters)
    page.total = total
    return page


async def get_tasks(snap: Snapshot, ids: list[str], view: View = "summary") -> TaskPage:
    """A batch read in the order asked; unknown ids come back in `missing`."""
    ids = list(dict.fromkeys(ids))
    if not 1 <= len(ids) <= MAX_IDS:
        raise ValidationFailedError(f"ids takes 1 to {MAX_IDS} task ids")
    async with snap.conn.execute(
        f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
        ids,
    ) as cur:
        found = {r["id"]: r for r in await cur.fetchall()}
    page = await _page(snap, [found[i] for i in ids if i in found], view)
    page.missing = [i for i in ids if i not in found]
    return page


async def refs(conn: aiosqlite.Connection, ids: list[str]) -> dict[str, Refs]:
    """Dependencies, open decisions and artifacts for many tasks, in 3 queries."""
    marks = ",".join("?" * len(ids))
    found: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {f: [] for f in Refs.model_fields}
    )
    for field, select, order in (
        (
            "depends_on",
            "SELECT task_id, depends_on_id FROM task_deps WHERE task_id",
            "depends_on_id",
        ),
        (
            "decision_ids",
            "SELECT subject_id, id FROM decisions"
            " WHERE subject_type = 'task' AND state = 'open' AND subject_id",
            "created_at, id",
        ),
        (
            "artifact_ids",
            "SELECT task_id, id FROM artifacts WHERE task_id",
            "attempt, created_at, id",
        ),
    ):
        sql = f"{select} IN ({marks}) ORDER BY {order}"
        async with conn.execute(sql, ids) as cur:
            for task_id, ref in await cur.fetchall():
                found[task_id][field].append(ref)
    return {i: Refs.model_validate(found[i]) for i in ids}


async def _page(snap: Snapshot, rows: list[aiosqlite.Row], view: View) -> TaskPage:
    by_id = await refs(snap.conn, [r["id"] for r in rows]) if rows else {}
    items: list[TaskSummary | TaskDetail] = []
    omitted: set[str] = set()
    for row in rows:
        truncated: dict[str, int] = {}
        common = {
            **by_id[row["id"]].model_dump(),
            "version": row["updated_at"],
            "age_s": _age(snap.now, row["created_at"]),
            "block_reason": cap(row["block_reason"], "block_reason", truncated),
            "progress_data": (
                json.loads(row["progress_data"]) if row["progress_data"] else None
            ),
            "progress_message": cap(
                row["progress_message"], "progress_message", truncated
            ),
        }
        if view == "summary":
            item: TaskSummary | TaskDetail = TaskSummary.model_validate(
                {**dict(row), **common, "truncated": truncated}
            )
        else:
            task = (await service.get_task(snap.conn, row["id"])).model_dump()
            task["spec"] = cap(task["spec"], "spec", truncated)
            task["error"] = cap(task["error"], "error", truncated)
            if task["result"] is not None:
                summary = cap(task["result"]["summary"], "result.summary", truncated)
                task["result"]["summary"] = summary
            item = TaskDetail.model_validate({**task, **common, "truncated": truncated})
        omitted |= truncated.keys()
        items.append(item)
    if view == "summary":
        omitted |= set(SUMMARY_OMITS)
    return TaskPage(
        items=items,
        total=len(items),
        has_more=False,
        next_cursor=None,
        omitted=sorted(omitted),
    )


def _age(now: datetime, since: str) -> int:
    return max(0, int((now - datetime.fromisoformat(since)).total_seconds()))
