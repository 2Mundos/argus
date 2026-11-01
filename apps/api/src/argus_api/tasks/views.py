"""Task lists and their projections (MCP operations 3.1, 3.2).

`summary` carries state and references (ids), never the spec or result bodies;
`detail` carries the whole task with each long free-text field cut to TEXT_CAP.
Optional `q` is literal substring search over title and spec
(docs/specs/task-discovery.spec.md).
"""

import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

import aiosqlite
from argus_core.model_trace import ModelsSummary
from argus_core.tasks import ProgressKind, Review, StallCause, Task, TaskState
from pydantic import BaseModel, Field

from argus_api.db import Snapshot
from argus_api.errors import ValidationFailedError
from argus_api.modeltrace import service as modeltrace
from argus_api.paging import (
    PageInfo,
    View,
    cap,
    clamp,
    decode_cursor,
    encode_cursor,
)
from argus_api.tasks import service
from argus_api.tasks.finalization import Finalization, newest

MAX_IDS = 50
MAX_QUERY_TERMS = 8
MAX_TERM_LEN = 64
SNIPPET_MAX = 200
# Larger than any stored spec; min(..., SENTINEL) means "no hit in spec".
_SPEC_POS_SENTINEL = 1_000_000_000
# Window large enough for _snippet centering when the hit is near the end.
_SPEC_WINDOW_PAD = SNIPPET_MAX
_SPEC_WINDOW_LEN = SNIPPET_MAX * 2


class Refs(BaseModel):
    depends_on: list[str]
    decision_ids: list[str]  # open decisions on the task
    artifact_ids: list[str]  # every attempt's, oldest first


class MatchHit(BaseModel):
    """A citation of where `q` hit; only present on list rows when `q` is set."""

    field: Literal["title", "spec"]
    snippet: str
    truncated: bool


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
    models: ModelsSummary = ModelsSummary()
    output_at: datetime | None = None
    created_at: datetime
    archived_at: datetime | None
    version: str  # updated_at; pass it back as expected_version
    age_s: int  # since creation
    truncated: dict[str, int] = {}  # field -> full length in bytes
    matches: list[MatchHit] | None = Field(default=None, exclude_if=lambda v: v is None)


class TaskDetail(Task, TaskSummary):
    pass


class TaskRead(Task):
    """The single-task read (stall visibility 3.4): the task plus its quiet facts."""

    quiet_at: datetime | None = None
    last_signal_at: datetime | None = None
    last_signal_source: str | None = None
    stall_cause: StallCause | None = None
    # Execution versus follow-up of the newest attempt (task finalization spec 3.4).
    finalization: Finalization | None = None


async def read_task(
    conn: aiosqlite.Connection, task_id: str, now: datetime
) -> TaskRead:
    task = await service.get_task(conn, task_id)
    facts = await service.quiet_facts(conn, task_id, now)
    return TaskRead(
        **task.model_dump(),
        quiet_at=facts.quiet_at,
        last_signal_at=facts.last_signal_at,
        last_signal_source=facts.last_signal_source,
        stall_cause=facts.stall_cause,
        finalization=await newest(conn, task_id, task.state, now),
    )


class TaskPage(PageInfo):
    items: list[TaskSummary | TaskDetail]
    missing: list[str] = Field(default_factory=list)  # requested ids not found


# What a summary leaves out of the full task.
SUMMARY_OMITS = sorted(
    set(Task.model_fields) - set(TaskSummary.model_fields) - {"updated_at"}
)

# Columns a list row needs. Bodies (spec, acceptance, input, result, error) stay
# in the table; view=detail loads them per id.
LIST_COLUMNS = (
    "id",
    "parent_id",
    "kind",
    "title",
    "project",
    "state",
    "priority",
    "review",
    "attempt",
    "max_attempts",
    "assignee_agent_id",
    "lease_expires_at",
    "block_reason",
    "progress_message",
    "progress_percent",
    "progress_at",
    "progress_kind",
    "progress_data",
    "output_at",
    "created_at",
    "archived_at",
    "updated_at",
)
_LIST_SQL = ", ".join(LIST_COLUMNS)


def parse_query(q: str | None) -> list[str] | None:
    """Whitespace-separated literal terms, or None when `q` was not passed."""
    if q is None:
        return None
    stripped = q.strip()
    if not stripped:
        raise ValidationFailedError("q is empty after strip", code="query_empty")
    terms = stripped.split()
    if len(terms) > MAX_QUERY_TERMS or any(len(t) > MAX_TERM_LEN for t in terms):
        raise ValidationFailedError(
            f"q takes at most {MAX_QUERY_TERMS} terms of {MAX_TERM_LEN} characters",
            code="query_too_broad",
        )
    return terms


def _like_term(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text: str, terms: list[str]) -> MatchHit | None:
    """Centred ≤ SNIPPET_MAX chars on the earliest case-insensitive hit, or None."""
    lower = text.lower()
    hit_at: int | None = None
    for term in terms:
        at = lower.find(term.lower())
        if at >= 0 and (hit_at is None or at < hit_at):
            hit_at = at
    if hit_at is None:
        return None
    if len(text) <= SNIPPET_MAX:
        return MatchHit(
            field="title", snippet=text, truncated=False
        )  # field set by caller
    half = SNIPPET_MAX // 2
    start = max(0, hit_at - half)
    end = min(len(text), start + SNIPPET_MAX)
    start = max(0, end - SNIPPET_MAX)
    body = text[start:end]
    if start > 0:
        body = "..." + body[3:] if len(body) > 3 else "..." + body
    if end < len(text):
        body = body[:-3] + "..." if len(body) > 3 else body + "..."
    return MatchHit(field="title", snippet=body, truncated=True)


def _spec_snippet_from_window(
    window: str | None,
    pos: int | None,
    spec_len: int,
    terms: list[str],
) -> MatchHit | None:
    """Build a spec citation from a SQL-bounded window around the earliest hit."""
    if window is None or pos is None or pos >= _SPEC_POS_SENTINEL:
        return None
    hit_at = pos - 1  # instr is 1-based
    if spec_len <= SNIPPET_MAX:
        raw = _snippet(window, terms)
        if raw is None:
            return None
        return MatchHit(field="spec", snippet=raw.snippet, truncated=False)
    half = SNIPPET_MAX // 2
    start = max(0, hit_at - half)
    end = min(spec_len, start + SNIPPET_MAX)
    start = max(0, end - SNIPPET_MAX)
    window_start = max(0, hit_at - _SPEC_WINDOW_PAD)
    body = window[start - window_start : end - window_start]
    if start > 0:
        body = "..." + body[3:] if len(body) > 3 else "..." + body
    if end < spec_len:
        body = body[:-3] + "..." if len(body) > 3 else body + "..."
    return MatchHit(field="spec", snippet=body, truncated=True)


def _match_hits_from_row(row: aiosqlite.Row, terms: list[str]) -> list[MatchHit]:
    hits: list[MatchHit] = []
    title_hit = _snippet(row["title"], terms)
    if title_hit is not None:
        hits.append(
            MatchHit(
                field="title", snippet=title_hit.snippet, truncated=title_hit.truncated
            )
        )
    spec_hit = _spec_snippet_from_window(
        row["_spec_window"],
        row["_spec_pos"],
        int(row["_spec_len"] or 0),
        terms,
    )
    if spec_hit is not None:
        hits.append(spec_hit)
    return hits


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
    task_scope: tuple[str, list[str]] | None = None,
    q: str | None = None,
) -> TaskPage:
    """Newest first, keyset-paged on (created_at, id). Archived tasks are left out
    unless `archived` asks for exactly those. `task_scope` is an optional SQL
    fragment (worker lease grants) that further restricts which tasks appear.
    `q` is optional literal substring search over title and spec."""
    limit = clamp(limit)
    terms = parse_query(q)
    filters: dict[str, Any] = {"states": sorted(states or []), "project": project}
    filters |= {"kind": kind, "assignee": assignee, "archived": archived}
    # Fingerprint `q` only when search is active, so cursors from builds without
    # q stay valid when the client still omits q.
    if terms is not None:
        filters["q"] = q
    where = ["archived_at IS NOT NULL" if archived else "archived_at IS NULL"]
    params: list[Any] = []
    if task_scope is not None:
        clause, scope_params = task_scope
        where.append(clause)
        params += scope_params
        filters["task_scope"] = scope_params
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
    if terms is not None:
        for term in terms:
            where.append(
                "(lower(title) LIKE '%' || lower(?) || '%' ESCAPE '\\'"
                " OR lower(spec) LIKE '%' || lower(?) || '%' ESCAPE '\\')"
            )
            liked = _like_term(term)
            params += [liked, liked]
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
    if terms is None:
        sql = (
            f"SELECT {_LIST_SQL} FROM tasks WHERE {' AND '.join(where)}"  # noqa: S608
            " ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        list_params: list[Any] = [*params, limit + 1]
    else:
        pos_parts = ", ".join("nullif(instr(lower(spec), lower(?)), 0)" for _ in terms)
        # min() with one arg is aggregate; always pass the sentinel too.
        pos_expr = f"min({pos_parts}, {_SPEC_POS_SENTINEL})"
        # SELECT placeholders (instr terms) bind before WHERE placeholders.
        sql = (
            f"SELECT {_LIST_SQL}, _spec_pos, _spec_len, "  # noqa: S608
            f"CASE WHEN _spec_pos >= {_SPEC_POS_SENTINEL} THEN NULL "
            f"ELSE substr(spec, max(1, _spec_pos - {_SPEC_WINDOW_PAD}), "
            f"{_SPEC_WINDOW_LEN}) END AS _spec_window "
            f"FROM (SELECT {_LIST_SQL}, spec, {pos_expr} AS _spec_pos, "
            f"length(spec) AS _spec_len FROM tasks "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?)"
        )
        list_params = [*terms, *params, limit + 1]
    async with conn.execute(sql, list_params) as cur:
        rows = list(await cur.fetchall())
    has_more = len(rows) > limit
    rows = rows[:limit]
    page = await _page(snap, rows, view, terms=terms)
    if has_more:
        last = rows[-1]
        page.has_more = True
        page.next_cursor = encode_cursor([last["created_at"], last["id"]], filters)
    page.total = total
    return page


async def get_tasks(
    snap: Snapshot,
    ids: list[str],
    view: View = "summary",
    *,
    task_scope: tuple[str, list[str]] | None = None,
) -> TaskPage:
    """A batch read in the order asked; unknown ids come back in `missing`.
    Out-of-scope ids for a worker are treated as missing (no existence leak)."""
    ids = list(dict.fromkeys(ids))
    if not 1 <= len(ids) <= MAX_IDS:
        raise ValidationFailedError(f"ids takes 1 to {MAX_IDS} task ids")
    sql = f"SELECT {_LIST_SQL} FROM tasks WHERE id IN ({','.join('?' * len(ids))})"  # noqa: S608
    params: list[Any] = list(ids)
    if task_scope is not None:
        clause, scope_params = task_scope
        sql += f" AND ({clause})"
        params += scope_params
    async with snap.conn.execute(sql, params) as cur:
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
            "SELECT task_id, id FROM artifacts WHERE source = 'worker' AND task_id",
            "attempt, created_at, id",
        ),
    ):
        sql = f"{select} IN ({marks}) ORDER BY {order}"
        async with conn.execute(sql, ids) as cur:
            for task_id, ref in await cur.fetchall():
                found[task_id][field].append(ref)
    return {i: Refs.model_validate(found[i]) for i in ids}


async def _page(
    snap: Snapshot,
    rows: list[aiosqlite.Row],
    view: View,
    *,
    terms: list[str] | None = None,
) -> TaskPage:
    ids = [r["id"] for r in rows]
    by_id = await refs(snap.conn, ids) if rows else {}
    models = await modeltrace.summaries(snap.conn, ids) if rows else {}
    items: list[TaskSummary | TaskDetail] = []
    omitted: set[str] = set()
    for row in rows:
        truncated: dict[str, int] = {}
        common: dict[str, Any] = {
            **by_id[row["id"]].model_dump(),
            "version": row["updated_at"],
            "models": models[row["id"]],
            "age_s": _age(snap.now, row["created_at"]),
            "block_reason": cap(row["block_reason"], "block_reason", truncated),
            "progress_data": (
                json.loads(row["progress_data"]) if row["progress_data"] else None
            ),
            "progress_message": cap(
                row["progress_message"], "progress_message", truncated
            ),
        }
        if terms is not None:
            common["matches"] = _match_hits_from_row(row, terms)
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
