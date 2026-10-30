"""Worker read scope: leased tasks and their projects (worker-read-scope spec).

Orchestrator and admin tokens keep global reads. Workers may only read tasks
(and artifacts) they currently or previously leased, and project-scoped
resources for projects of those tasks.
"""

from __future__ import annotations

from collections.abc import Collection

import aiosqlite
from argus_core.auth import Scope

from argus_api.auth import Identity
from argus_api.db import Tx, ts
from argus_api.errors import ForbiddenError

GLOBAL_READERS = frozenset({Scope.ORCHESTRATOR, Scope.ADMIN})


def is_global_reader(caller: Identity) -> bool:
    return caller.scope in GLOBAL_READERS


def _worker_id(caller: Identity) -> str | None:
    """Agent id when the caller is a scoped worker; None for managers."""
    if is_global_reader(caller):
        return None
    if caller.agent_id is None:
        raise ForbiddenError(
            "requires an agent token; admin tokens are not agents",
            code="forbidden",
        )
    return caller.agent_id


async def record_lease_grant(tx: Tx, task_id: str, agent_id: str, attempt: int) -> None:
    """Remember that this agent leased this attempt (idempotent per agent and attempt; retry resets
    `attempt`, so another agent may lease the same number)."""
    await tx.conn.execute(
        "INSERT OR IGNORE INTO task_lease_grants"
        " (task_id, agent_id, attempt, leased_at) VALUES (?, ?, ?, ?)",
        (task_id, agent_id, attempt, ts(tx.now)),
    )


async def has_leased_task(
    conn: aiosqlite.Connection, agent_id: str, task_id: str
) -> bool:
    async with conn.execute(
        "SELECT 1 FROM task_lease_grants WHERE agent_id = ? AND task_id = ? LIMIT 1",
        (agent_id, task_id),
    ) as cur:
        return await cur.fetchone() is not None


async def leased_projects(conn: aiosqlite.Connection, agent_id: str) -> set[str]:
    async with conn.execute(
        "SELECT DISTINCT t.project FROM task_lease_grants g"
        " JOIN tasks t ON t.id = g.task_id WHERE g.agent_id = ?",
        (agent_id,),
    ) as cur:
        return {r[0] for r in await cur.fetchall()}


async def require_task_readable(
    conn: aiosqlite.Connection, caller: Identity, task_id: str
) -> None:
    agent_id = _worker_id(caller)
    if agent_id is None:
        return
    async with conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)) as cur:
        if await cur.fetchone() is None:
            return  # let the handler raise not_found
    if not await has_leased_task(conn, agent_id, task_id):
        raise ForbiddenError(
            "workers may only read tasks they have leased",
            code="not_leased",
        )


async def require_project_readable(
    conn: aiosqlite.Connection, caller: Identity, project: str
) -> None:
    agent_id = _worker_id(caller)
    if agent_id is None:
        return
    if project not in await leased_projects(conn, agent_id):
        raise ForbiddenError(
            "workers may only read knowledge for projects of tasks they have leased",
            code="project_scope",
        )


async def require_page_readable(
    conn: aiosqlite.Connection, caller: Identity, page_id: str, project: str
) -> None:
    """A page is readable when its project is leased, or when a task the worker
    leased pins it (pinned pages may live in another project, e.g. `global`)."""
    agent_id = _worker_id(caller)
    if agent_id is None:
        return
    async with conn.execute(
        "SELECT 1 FROM task_knowledge k JOIN task_lease_grants g"
        " ON g.task_id = k.task_id WHERE k.page_id = ? AND g.agent_id = ? LIMIT 1",
        (page_id, agent_id),
    ) as cur:
        if await cur.fetchone() is not None:
            return
    await require_project_readable(conn, caller, project)


async def require_decision_readable(
    conn: aiosqlite.Connection, caller: Identity, decision_id: str
) -> None:
    agent_id = _worker_id(caller)
    if agent_id is None:
        return
    async with conn.execute(
        "SELECT d.subject_type, d.subject_id,"
        " coalesce(t.project, kp.project) AS project"
        " FROM decisions d"
        " LEFT JOIN tasks t ON d.subject_type = 'task' AND t.id = d.subject_id"
        " LEFT JOIN knowledge_proposals kp"
        "  ON d.subject_type = 'knowledge_proposal' AND kp.id = d.subject_id"
        " WHERE d.id = ?",
        (decision_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return  # let the store raise not_found
    if row["subject_type"] == "task":
        if not await has_leased_task(conn, agent_id, row["subject_id"]):
            raise ForbiddenError(
                "workers may only read decisions for tasks they have leased",
                code="not_leased",
            )
        return
    project = row["project"]
    if project is None or project not in await leased_projects(conn, agent_id):
        raise ForbiddenError(
            "workers may only read decisions for projects of tasks they have leased",
            code="project_scope",
        )


def worker_task_filter(
    caller: Identity,
) -> tuple[str, list[str]] | None:
    """SQL fragment restricting tasks to the caller's lease grants, or None when
    the caller may see every task."""
    if is_global_reader(caller):
        return None
    if caller.agent_id is None:
        raise ForbiddenError(
            "requires an agent token; admin tokens are not agents",
            code="forbidden",
        )
    return (
        "id IN (SELECT task_id FROM task_lease_grants WHERE agent_id = ?)",
        [caller.agent_id],
    )


def worker_decision_filter(
    caller: Identity,
) -> tuple[str, list[str]] | None:
    """Decisions on leased tasks, or knowledge proposals in leased projects."""
    if is_global_reader(caller):
        return None
    if caller.agent_id is None:
        raise ForbiddenError(
            "requires an agent token; admin tokens are not agents",
            code="forbidden",
        )
    return (
        "("
        " (d.subject_type = 'task' AND d.subject_id IN ("
        "   SELECT task_id FROM task_lease_grants WHERE agent_id = ?))"
        " OR (d.subject_type = 'knowledge_proposal' AND kp.project IN ("
        "   SELECT DISTINCT t.project FROM task_lease_grants g"
        "   JOIN tasks t ON t.id = g.task_id WHERE g.agent_id = ?))"
        ")",
        [caller.agent_id, caller.agent_id],
    )


def worker_project_filter(
    caller: Identity,
    projects: Collection[str] | None = None,
    *,
    column: str = "project",
) -> tuple[str, list[str]] | None:
    """SQL fragment restricting rows by a project column. `projects` is the
    caller's allowed set when already loaded; None means a subquery on grants.
    Returns None for global readers. `column` is `project` or `p.project`."""
    if is_global_reader(caller):
        return None
    if caller.agent_id is None:
        raise ForbiddenError(
            "requires an agent token; admin tokens are not agents",
            code="forbidden",
        )
    if projects is not None:
        allowed = list(projects)
        if not allowed:
            return ("0", [])
        marks = ", ".join("?" * len(allowed))
        if column == "p.project":
            return (f"p.project IN ({marks})", allowed)
        if column == "project":
            return (f"project IN ({marks})", allowed)
        raise ValueError(f"unsupported project column: {column}")
    subquery = (
        "SELECT DISTINCT t.project FROM task_lease_grants g"
        " JOIN tasks t ON t.id = g.task_id WHERE g.agent_id = ?"
    )
    if column == "p.project":
        return (f"p.project IN ({subquery})", [caller.agent_id])
    if column == "project":
        return (f"project IN ({subquery})", [caller.agent_id])
    raise ValueError(f"unsupported project column: {column}")


def worker_event_filter(
    caller: Identity, projects: Collection[str]
) -> tuple[str, list[str]] | None:
    """Limit events to what the worker may read. Managers: None.

    Task events (and decisions on tasks) need a lease grant for that very task;
    project, knowledge page, and knowledge-proposal decision events need the
    subject's project to be in `projects`. Everything else is fleet-wide and
    hidden. `task.attachment` events are hidden too: their actor is the
    uploader's principal."""
    if is_global_reader(caller):
        return None
    agent_id = _worker_id(caller)
    assert agent_id is not None  # non-global callers always have one
    leased = "SELECT task_id FROM task_lease_grants WHERE agent_id = ?"
    if not projects:
        # No leased projects means no grants, hence nothing readable.
        return ("0", [])
    marks = ", ".join("?" * len(projects))
    allowed = list(projects)
    # `marks` is only "?" placeholders; values are bound separately.
    in_projects = " IN (" + marks + ")"
    clause = (
        "(subject_type = 'task' AND kind != 'task.attachment'"  # noqa: S608
        "  AND subject_id IN (" + leased + "))"
        " OR (subject_type = 'project' AND subject_id" + in_projects + ")"
        " OR (subject_type = 'knowledge_page' AND subject_id IN ("
        "  SELECT id FROM knowledge_pages WHERE project" + in_projects + "))"
        " OR (subject_type = 'decision' AND subject_id IN ("
        "  SELECT d.id FROM decisions d"
        "  LEFT JOIN knowledge_proposals kp"
        "   ON d.subject_type = 'knowledge_proposal' AND kp.id = d.subject_id"
        "  WHERE (d.subject_type = 'task' AND d.subject_id IN (" + leased + "))"
        "   OR kp.project" + in_projects + "))"
    )
    return (
        "(" + clause + ")",
        [agent_id, *allowed, *allowed, agent_id, *allowed],
    )


async def require_artifact_readable(
    conn: aiosqlite.Connection,
    caller: Identity,
    task_id: str,
    source: str,
    worker_readable: bool = False,
) -> None:
    """Attachments (orchestrator evidence) are for global readers only, unless
    attached with `worker_readable`; worker artifacts and shared attachments follow
    the task rule (a lease grant on that task)."""
    if source == "attachment" and not worker_readable and not is_global_reader(caller):
        raise ForbiddenError(
            "attachments are readable by orchestrators and admins only",
            code="attachment_scope",
        )
    await require_task_readable(conn, caller, task_id)
