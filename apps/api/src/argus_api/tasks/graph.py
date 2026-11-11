"""A project's task graph: one read-only snapshot (task graph spec 3.1)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiosqlite
from argus_core.tasks import TaskState
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from argus_api.access import require_project_readable, worker_task_filter
from argus_api.db import Snapshot, ts
from argus_api.deps import CallerDep, DbDep
from argus_api.errors import NotFoundError

MAX_NODES = 500
_BATCH = 500
DONE_AFTER = timedelta(days=7)
_COLUMNS = "id, title, state, kind, priority, block_reason, project, created_at"

router = APIRouter(tags=["tasks"])


class GraphNode(BaseModel):
    id: str
    title: str
    state: TaskState
    kind: str
    priority: int
    block_reason: str | None
    failed_dependencies: list[str]  # of a pending node: failed or cancelled
    project: str  # differs from the requested project on a cross-project stub


class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")  # the dependency
    to: str  # the task that waits for it


class TaskGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool  # some task was left out because of the node cap


async def task_graph(
    snap: Snapshot,
    project: str,
    *,
    include_done: bool = False,
    task_scope: tuple[str, list[str]] | None = None,
) -> TaskGraph:
    """The project's non-archived tasks, newest first, each with its dependencies,
    until the next one would not fit in `MAX_NODES`; that task and all older ones
    are left out and `truncated` is set. A dependency in another project is a stub
    node (not expanded). `task_scope` (a worker's leases) applies before anything
    is selected: a task or dependency out of scope is never named, and its edge is
    dropped. Archived dependencies are dropped the same way."""
    where = ["project = ?", "archived_at IS NULL"]
    params: list[Any] = [project]
    if task_scope is not None:
        where.append(task_scope[0])
        params += task_scope[1]
    if not include_done:
        # An old succeeded task stays while a dependent (in scope) is unfinished.
        dependents_scope, dependents_params = _in_scope(task_scope, "u")
        where.append(
            "NOT (state = 'succeeded' AND updated_at < ? AND NOT EXISTS ("  # noqa: S608
            " SELECT 1 FROM task_deps d JOIN tasks u ON u.id = d.task_id"
            " WHERE d.depends_on_id = tasks.id AND u.archived_at IS NULL"
            f" AND u.state != 'succeeded'{dependents_scope}))"
        )
        params.append(ts(snap.now - DONE_AFTER))
        params += dependents_params
    sql = (
        f"SELECT id FROM tasks WHERE {' AND '.join(where)}"  # noqa: S608
        " ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    async with snap.conn.execute(sql, [*params, MAX_NODES + 1]) as cur:
        candidates = [r["id"] for r in await cur.fetchall()]
    truncated = len(candidates) > MAX_NODES

    graph = _Graph(snap.conn, project, task_scope)
    await graph.load(candidates)
    chosen: dict[str, None] = {}
    for task_id in candidates:
        need = await graph.closure(task_id, chosen)
        if len(chosen) + len(need) > MAX_NODES:
            truncated = True
            break
        chosen.update(dict.fromkeys(need))

    rows = sorted(
        (graph.rows[i] for i in chosen), key=lambda r: (r["created_at"], r["id"])
    )
    edges = sorted(
        (
            GraphEdge(from_=dep, to=task_id)
            for task_id in chosen
            if graph.rows[task_id]["project"] == project
            for dep in graph.deps[task_id]
        ),
        key=lambda e: (e.from_, e.to),
    )
    return TaskGraph(
        nodes=[graph.node(r) for r in rows], edges=edges, truncated=truncated
    )


def _in_scope(
    task_scope: tuple[str, list[str]] | None, alias: str
) -> tuple[str, list[str]]:
    """The worker scope clause (` AND <alias>.id IN (...)`) for a joined table."""
    if task_scope is None:
        return "", []
    return " AND " + task_scope[0].replace("id IN", f"{alias}.id IN", 1), task_scope[1]


class _Graph:
    """Task rows and visible dependencies, loaded in batches and cached."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        project: str,
        task_scope: tuple[str, list[str]] | None,
    ) -> None:
        self._conn = conn
        self._project = project
        self._scope = task_scope
        self.rows: dict[str, aiosqlite.Row] = {}
        self.deps: dict[str, list[str]] = {}  # in-project tasks only

    async def load(self, ids: list[str]) -> None:
        """Rows of `ids`, then the dependency ids of the in-project ones (in
        scope, not archived) and the rows behind them."""
        await self._rows([i for i in ids if i not in self.rows])
        mine = [
            i
            for i in ids
            if self.rows[i]["project"] == self._project and i not in self.deps
        ]
        for start in range(0, len(mine), _BATCH):
            batch = mine[start : start + _BATCH]
            scope, scope_params = _in_scope(self._scope, "u")
            sql = (
                "SELECT d.task_id, d.depends_on_id FROM task_deps d"  # noqa: S608
                " JOIN tasks u ON u.id = d.depends_on_id"
                f" WHERE d.task_id IN ({','.join('?' * len(batch))})"
                f" AND u.archived_at IS NULL{scope}"
                " ORDER BY d.task_id, d.depends_on_id"
            )
            self.deps.update({i: [] for i in batch})
            async with self._conn.execute(sql, [*batch, *scope_params]) as cur:
                for r in await cur.fetchall():
                    self.deps[r["task_id"]].append(r["depends_on_id"])
            behind = {d for i in batch for d in self.deps[i]}
            await self._rows(sorted(behind - self.rows.keys()))

    async def _rows(self, ids: list[str]) -> None:
        for start in range(0, len(ids), _BATCH):
            batch = ids[start : start + _BATCH]
            sql = (
                f"SELECT {_COLUMNS} FROM tasks"  # noqa: S608
                f" WHERE id IN ({','.join('?' * len(batch))})"
            )
            async with self._conn.execute(sql, batch) as cur:
                for r in await cur.fetchall():
                    self.rows[r["id"]] = r

    async def closure(self, task_id: str, have: dict[str, None]) -> list[str]:
        """`task_id` and the in-project tasks it depends on, transitively, that
        are not in `have`, plus the foreign tasks directly behind them."""
        need: dict[str, None] = {}
        todo = [task_id]
        while todo:
            current = todo.pop()
            if current in have or current in need:
                continue
            need[current] = None
            await self.load([current])
            if self.rows[current]["project"] == self._project:
                todo += self.deps[current]
        return list(need)

    def node(self, row: aiosqlite.Row) -> GraphNode:
        foreign = row["project"] != self._project
        failed = (
            []
            if foreign or row["state"] != TaskState.PENDING
            else [
                d
                for d in self.deps[row["id"]]
                if self.rows[d]["state"] in (TaskState.FAILED, TaskState.CANCELLED)
            ]
        )
        return GraphNode(
            id=row["id"],
            title=row["title"],
            state=row["state"],
            kind=row["kind"],
            priority=row["priority"],
            block_reason=None if foreign else row["block_reason"],
            failed_dependencies=failed,
            project=row["project"],
        )


@router.get("/projects/{name}/task-graph")
async def get_task_graph(
    name: str, db: DbDep, caller: CallerDep, include_done: bool = False
) -> TaskGraph:
    """The project's tasks as nodes and dependencies as edges (`from` is the
    dependency, `to` the task waiting for it), read in one snapshot, at most 500
    nodes, newest first with their dependencies; `truncated` says some were left
    out. Archived tasks and tasks `succeeded` over 7 days ago with no unfinished
    dependent are left out unless `include_done=true`. A dependency in another
    project is a stub node. Workers only see tasks they have leased."""
    scope = worker_task_filter(caller)
    async with db.snapshot() as snap:
        await require_project_readable(snap.conn, caller, name)
        async with snap.conn.execute(
            "SELECT 1 FROM projects WHERE slug = ?", (name,)
        ) as cur:
            if await cur.fetchone() is None:
                raise NotFoundError(f"project not found: {name}")
        return await task_graph(snap, name, include_done=include_done, task_scope=scope)
