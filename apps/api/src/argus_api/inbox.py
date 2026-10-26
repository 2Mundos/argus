"""What waits on a manager, in one page (MCP operations 3.2).

Rows are tasks that are blocked, in review or awaiting approval, each joined
with its open decision if any; pending tasks stuck behind a failed or cancelled
dependency (task dependencies 3.2); and open decisions about other subjects
(pools, knowledge proposals). Totals come from the same snapshot as the page.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from argus_core.decisions import Audience, DecisionKind
from argus_core.tasks import TaskState
from pydantic import BaseModel

from argus_api.db import Snapshot
from argus_api.decisions import store as decisions
from argus_api.decisions.views import DecisionDetail, project_row
from argus_api.paging import (
    PageInfo,
    View,
    clamp,
    decode_cursor,
    encode_cursor,
)
from argus_api.tasks import service as tasks_service
from argus_api.tasks import views as tasks
from argus_api.tasks.views import TaskDetail

Order = Literal["age", "impact"]


class BlockedTask(BaseModel):
    id: str
    title: str
    state: TaskState


class InboxItem(BaseModel):
    subject_type: str  # task, pool, knowledge_proposal
    subject_id: str
    title: str | None
    project: str | None  # None for pools
    state: TaskState | None  # tasks only
    kind: str | None  # the task's kind
    attempt: int | None
    version: str | None  # the task's updated_at
    decision_id: str | None  # the open decision, if any
    decision_kind: DecisionKind | None
    audience: Audience | None  # who may answer it
    age_s: int  # since it started waiting
    group: str  # the InboxGroup.key it belongs to
    blocks: list[BlockedTask]  # tasks that cannot proceed until this is resolved
    impact: int  # len(blocks)
    artifact_ids: list[str]  # the current attempt's
    failed_dependencies: list[str]  # failed or cancelled; a pending task waits on them
    last_signal_at: datetime | None = (
        None  # quiet claims: newest activity/progress/log/claim
    )
    last_signal_source: str | None = None  # activity, progress, log, or claim
    task: TaskDetail | None = None  # view=detail only
    decision: DecisionDetail | None = None  # view=detail only


class InboxGroup(BaseModel):
    key: str  # task:<id>, pool, or knowledge_proposal:<project>
    project: str | None
    subject_type: str
    subject_id: str | None  # the task; None for pool and knowledge groups
    title: str | None
    open_decisions: int
    oldest_age_s: int
    stalled_tasks: int  # distinct tasks blocked by the group's rows


class InboxPage(PageInfo):
    items: list[InboxItem]
    groups: list[InboxGroup]  # every group of the filtered set, not just the page
    totals_by_state: dict[str, int]  # task state, else the subject type
    totals_by_decision_kind: dict[str, int]  # "none": no open decision


_ROWS = (
    "SELECT 'task' AS subject_type, t.id AS subject_id, t.title, t.project,"
    " t.state, t.kind, t.attempt, t.updated_at AS version, d.id AS decision_id,"
    " d.kind AS decision_kind, d.audience,"
    " coalesce(d.created_at, t.updated_at) AS since,"
    " coalesce(d.id, t.id) AS row_key,"
    " NULL AS last_signal_at"
    " FROM tasks t LEFT JOIN decisions d ON d.subject_type = 'task'"
    "  AND d.subject_id = t.id AND d.state = 'open'"
    " WHERE t.state IN ('blocked', 'in_review', 'awaiting_approval')"
    " UNION ALL"
    " SELECT 'task', t.id, t.title, t.project, t.state, t.kind, t.attempt,"
    " t.updated_at, NULL, NULL, NULL, max(u.updated_at), t.id, NULL"
    " FROM tasks t JOIN task_deps td ON td.task_id = t.id"
    " JOIN tasks u ON u.id = td.depends_on_id"
    " WHERE t.state = 'pending' AND u.state IN ('failed', 'cancelled')"
    " GROUP BY t.id"
    " UNION ALL"
    # Quiet claimed tasks (stall visibility): waiting since quiet_at.
    " SELECT 'task', t.id, t.title, t.project, t.state, t.kind, t.attempt,"
    " t.updated_at, NULL, NULL, NULL, t.quiet_at, t.id,"
    " NULL"
    " FROM tasks t"
    " WHERE t.state = 'claimed' AND t.quiet_at IS NOT NULL"
    " UNION ALL"
    " SELECT d.subject_type, d.subject_id, coalesce(kp.title, d.subject_id),"
    " kp.project, NULL, NULL, NULL, NULL, d.id, d.kind, d.audience,"
    " d.created_at, d.id, NULL"
    " FROM decisions d LEFT JOIN knowledge_proposals kp"
    "  ON d.subject_type = 'knowledge_proposal' AND kp.id = d.subject_id"
    " WHERE d.state = 'open' AND d.subject_type != 'task'"
)

# `impact`: the row's own task when blocked, plus its non-terminal direct dependents.
_WITH_IMPACT = (
    "SELECT r.*, coalesce(r.state = 'blocked', 0) + (SELECT count(*) FROM task_deps td"  # noqa: S608
    " JOIN tasks c ON c.id = td.task_id WHERE r.subject_type = 'task'"
    " AND td.depends_on_id = r.subject_id"
    " AND c.state NOT IN ('succeeded', 'failed', 'cancelled')) AS impact"
    f" FROM ({_ROWS}) r"
)
_DEPENDENTS = (
    "SELECT td.depends_on_id, c.id, c.title, c.state FROM task_deps td"
    " JOIN tasks c ON c.id = td.task_id"
    " WHERE c.state NOT IN ('succeeded', 'failed', 'cancelled')"
    " ORDER BY c.created_at, c.id"
)

_TOTALS = "coalesce(state, subject_type), coalesce(decision_kind, 'none'), count(*)"
_CURRENT_ARTIFACTS = (
    "SELECT a.task_id, a.id FROM artifacts a JOIN tasks t ON t.id = a.task_id"
    " WHERE a.attempt = t.attempt AND a.source = 'worker'"
)


async def inbox(
    snap: Snapshot,
    *,
    project: str | None = None,
    states: list[TaskState] | None = None,
    decision_kinds: list[DecisionKind] | None = None,
    blocking_only: bool = False,
    order: Order = "age",
    view: View = "summary",
    limit: int = 25,
    cursor: str | None = None,
) -> InboxPage:
    """Oldest first, or most impactful first with `order="impact"`. `states` keeps
    only task rows in those states; `blocking_only` keeps rows that block a task."""
    limit = clamp(limit)
    filters: dict[str, Any] = {"project": project, "states": sorted(states or [])}
    filters |= {"decision_kinds": sorted(decision_kinds or [])}
    filters |= {"blocking_only": blocking_only, "order": order}
    where, params = ["1 = 1"], []
    if blocking_only:
        where.append("impact >= 1")
    if project is not None:
        where.append("project = ?")
        params.append(project)
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        params += states
    if decision_kinds:
        where.append(f"decision_kind IN ({','.join('?' * len(decision_kinds))})")
        params += decision_kinds
    rows_sql = f"SELECT * FROM ({_WITH_IMPACT}) WHERE {' AND '.join(where)}"  # noqa: S608
    conn = snap.conn
    by_state: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    totals_sql = f"SELECT {_TOTALS} FROM ({rows_sql}) GROUP BY 1, 2"  # noqa: S608
    async with conn.execute(totals_sql, params) as cur:
        for state, kind, n in await cur.fetchall():
            by_state[state] += n
            by_kind[kind] += n
    sort = "-impact, since, row_key" if order == "impact" else "since, row_key"
    async with conn.execute(
        f"SELECT * FROM ({rows_sql}) ORDER BY {sort}",  # noqa: S608
        params,
    ) as cur:
        everything = list(await cur.fetchall())
    dependents = await _dependents(snap)
    groups = _groups(snap, everything, dependents, order)
    if (after := decode_cursor(cursor, filters)) is not None:
        rows_sql += f" AND ({sort}) > ({','.join('?' * len(after))})"
        params += after
    async with conn.execute(
        f"{rows_sql} ORDER BY {sort} LIMIT ?", [*params, limit + 1]
    ) as cur:
        rows = list(await cur.fetchall())
    has_more = len(rows) > limit
    rows = rows[:limit]

    task_ids = [r["subject_id"] for r in rows if r["subject_type"] == "task"]
    artifacts = await _current_artifacts(snap, task_ids)
    stuck = await _failed_dependencies(
        snap, [r["subject_id"] for r in rows if r["state"] == TaskState.PENDING]
    )
    details: dict[str, TaskDetail] = {}
    if view == "detail" and task_ids:
        page = await tasks.get_tasks(snap, task_ids, "detail")
        details = {t.id: t for t in page.items if isinstance(t, TaskDetail)}
    quiet_ids = [
        r["subject_id"]
        for r in rows
        if r["subject_type"] == "task" and r["state"] == TaskState.CLAIMED
    ]
    last_signals = await _quiet_last_signals(snap.conn, quiet_ids)
    items: list[InboxItem] = []
    omitted: set[str] = set()
    for r in rows:
        item = InboxItem.model_validate(
            {
                **dict(r),
                "age_s": _age(snap, r),
                "group": _group_key(r),
                "blocks": (blocks := _blocks(r, dependents)),
                "impact": len(blocks),
                "artifact_ids": artifacts.get(r["subject_id"], []),
                "failed_dependencies": stuck.get(r["subject_id"], []),
            }
        )
        if sig := last_signals.get(r["subject_id"]):
            at, source = sig
            item.last_signal_at = at
            item.last_signal_source = source
        if view == "detail":
            item.task = details.get(r["subject_id"])
            if r["decision_id"] is not None:
                decision = project_row(
                    snap.now, await _decision_row(snap, r["decision_id"]), "detail"
                )
                assert isinstance(decision, DecisionDetail)
                item.decision = decision
            for part, prefix in ((item.task, "task."), (item.decision, "decision.")):
                if part is not None:
                    omitted |= {prefix + f for f in part.truncated}
        items.append(item)
    if view == "summary":
        omitted |= {"task", "decision"}
    last = rows[-1] if has_more else None
    return InboxPage(
        items=items,
        total=sum(by_state.values()),
        has_more=has_more,
        next_cursor=encode_cursor(_cursor_key(last, order), filters)
        if last is not None
        else None,
        omitted=sorted(omitted),
        groups=groups,
        totals_by_state=dict(sorted(by_state.items())),
        totals_by_decision_kind=dict(sorted(by_kind.items())),
    )


def _cursor_key(row: Any, order: Order) -> list[Any]:
    key = [row["since"], row["row_key"]]
    return [-row["impact"], *key] if order == "impact" else key


def _age(snap: Snapshot, row: Any) -> int:
    return max(0, int((snap.now - _at(row["since"])).total_seconds()))


def _group_key(row: Any) -> str:
    if row["subject_type"] == "task":
        return f"task:{row['subject_id']}"
    if row["subject_type"] == "knowledge_proposal":
        return f"knowledge_proposal:{row['project']}"
    return row["subject_type"]


async def _dependents(snap: Snapshot) -> dict[str, list[BlockedTask]]:
    """Non-terminal tasks that depend on each task, from task_deps."""
    found: dict[str, list[BlockedTask]] = defaultdict(list)
    async with snap.conn.execute(_DEPENDENTS) as cur:
        for parent, task_id, title, state in await cur.fetchall():
            found[parent].append(BlockedTask(id=task_id, title=title, state=state))
    return found


def _blocks(row: Any, dependents: dict[str, list[BlockedTask]]) -> list[BlockedTask]:
    if row["subject_type"] != "task":
        return []
    own = []
    if row["state"] == TaskState.BLOCKED:
        own = [
            BlockedTask(id=row["subject_id"], title=row["title"], state=row["state"])
        ]
    return own + dependents.get(row["subject_id"], [])


def _groups(
    snap: Snapshot,
    rows: list[Any],
    dependents: dict[str, list[BlockedTask]],
    order: Order,
) -> list[InboxGroup]:
    """One group per task, per project's knowledge proposals, and for pools."""
    members: dict[str, list[Any]] = {}
    for r in rows:
        members.setdefault(_group_key(r), []).append(r)
    groups = []
    for key, group_rows in members.items():
        first = group_rows[0]
        is_task = first["subject_type"] == "task"
        stalled = {b.id for r in group_rows for b in _blocks(r, dependents)}
        groups.append(
            InboxGroup(
                key=key,
                project=None if first["subject_type"] == "pool" else first["project"],
                subject_type=first["subject_type"],
                subject_id=first["subject_id"] if is_task else None,
                title=first["title"] if is_task else None,
                open_decisions=sum(r["decision_id"] is not None for r in group_rows),
                oldest_age_s=max(_age(snap, r) for r in group_rows),
                stalled_tasks=len(stalled),
            )
        )
    if order == "impact":
        groups.sort(key=lambda g: (-g.stalled_tasks, -g.oldest_age_s, g.key))
    else:
        groups.sort(key=lambda g: (-g.oldest_age_s, g.key))
    return groups


async def _current_artifacts(snap: Snapshot, task_ids: list[str]) -> dict[str, list]:
    """Artifact ids of each task's current attempt: the evidence under review."""
    if not task_ids:
        return {}
    found: dict[str, list[str]] = defaultdict(list)
    marks = ",".join("?" * len(task_ids))
    sql = f"{_CURRENT_ARTIFACTS} AND a.task_id IN ({marks})"
    async with snap.conn.execute(sql + " ORDER BY a.created_at, a.id", task_ids) as cur:
        for task_id, artifact_id in await cur.fetchall():
            found[task_id].append(artifact_id)
    return found


async def _failed_dependencies(
    snap: Snapshot, task_ids: list[str]
) -> dict[str, list[str]]:
    if not task_ids:
        return {}
    found: dict[str, list[str]] = defaultdict(list)
    marks = ",".join("?" * len(task_ids))
    sql = f"{_FAILED_DEPS} AND d.task_id IN ({marks})"
    async with snap.conn.execute(sql + " ORDER BY u.id", task_ids) as cur:
        for task_id, dep_id in await cur.fetchall():
            found[task_id].append(dep_id)
    return found


async def _quiet_last_signals(
    conn: Any, task_ids: list[str]
) -> dict[str, tuple[datetime | None, str | None]]:
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    sql = f"{_QUIET_SIGNALS} WHERE t.id IN ({marks})"
    async with conn.execute(sql, task_ids) as cur:
        rows = await cur.fetchall()
    return {
        r["id"]: tasks_service.last_signal(
            activity_at=r["activity_at"],
            progress_at=r["progress_at"],
            output_at=r["output_at"],
            claimed_at=r["claimed_at"],
        )
        for r in rows
    }


_QUIET_SIGNALS = (
    "SELECT t.id, t.activity_at, t.progress_at, t.output_at, a.claimed_at"
    " FROM tasks t"
    " LEFT JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
)

_FAILED_DEPS = (
    "SELECT d.task_id, u.id FROM task_deps d JOIN tasks u ON u.id = d.depends_on_id"
    " WHERE u.state IN ('failed', 'cancelled')"
)


async def _decision_row(snap: Snapshot, decision_id: str) -> Any:
    async with snap.conn.execute(
        decisions.SELECT + " WHERE d.id = ?", (decision_id,)
    ) as cur:
        return await cur.fetchone()


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)
