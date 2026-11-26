"""The active queue in one bounded read (MCP operations 3.2): every task that is
blocked, claimed, ready, in review or awaiting approval, with its open decision.
Terminal and pending tasks never appear, so the size follows the live work."""

from typing import Literal

from argus_core.decisions import DecisionKind
from argus_core.redact import redact
from argus_core.tasks import TaskState
from pydantic import BaseModel

from argus_api.db import Snapshot
from argus_api.paging import SUMMARY_CHARS

QueueState = Literal["blocked", "claimed", "ready", "in_review", "awaiting_approval"]
QUEUE_STATES: tuple[QueueState, ...] = (
    "blocked",
    "claimed",
    "ready",
    "in_review",
    "awaiting_approval",
)
MAX_ROWS = 200
TITLE_CHARS = 120
REASON_CHARS = 160


class QueueDecision(BaseModel):
    id: str  # the full id, what answer_decision takes
    kind: DecisionKind
    question: str  # cut to SUMMARY_CHARS


class QueueTask(BaseModel):
    id: str
    project: str
    state: TaskState
    attempt: int
    failures: int
    max_attempts: int
    block_reason: str | None
    title: str
    decision: QueueDecision | None = None


class Queue(BaseModel):
    items: list[QueueTask]
    totals_by_state: dict[str, int]  # every match, also past `items`
    truncated: bool  # more than MAX_ROWS match; narrow with project or states


def _cut(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = redact(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def get_queue(
    snap: Snapshot,
    *,
    project: str | None = None,
    states: list[QueueState] | None = None,
) -> Queue:
    wanted = list(states or QUEUE_STATES)
    where = [f"state IN ({','.join('?' * len(wanted))})", "archived_at IS NULL"]
    params: list[str | int] = [*wanted]
    if project is not None:
        where.append("project = ?")
        params.append(project)
    clause = " AND ".join(where)
    sql = f"SELECT state, count(*) FROM tasks WHERE {clause} GROUP BY state"  # noqa: S608
    async with snap.conn.execute(sql, params) as cur:
        found_totals = {r[0]: r[1] for r in await cur.fetchall()}
    totals: dict[str, int] = {state: found_totals.get(state, 0) for state in wanted}
    sql = (
        "SELECT id, project, state, attempt, failures, max_attempts, block_reason,"  # noqa: S608
        f" title FROM tasks WHERE {clause} ORDER BY created_at, id LIMIT ?"
    )
    async with snap.conn.execute(sql, [*params, MAX_ROWS]) as cur:
        rows = list(await cur.fetchall())
    found: dict[str, QueueDecision] = {}
    if rows:
        marks = ",".join("?" * len(rows))
        # Newest first, so the oldest open decision overwrites and wins.
        sql = (
            "SELECT subject_id, id, kind, question FROM decisions"  # noqa: S608
            f" WHERE subject_type = 'task' AND state = 'open' AND subject_id IN ({marks})"
            " ORDER BY created_at DESC, id DESC"
        )
        async with snap.conn.execute(sql, [r["id"] for r in rows]) as cur:
            for task_id, decision_id, kind, question in await cur.fetchall():
                found[task_id] = QueueDecision(
                    id=decision_id,
                    kind=kind,
                    question=_cut(question, SUMMARY_CHARS) or "",
                )
    items = [
        QueueTask(
            **{f: r[f] for f in ("id", "project", "state", "attempt", "failures")},
            max_attempts=r["max_attempts"],
            block_reason=_cut(r["block_reason"], REASON_CHARS),
            title=_cut(r["title"], TITLE_CHARS) or "",
            decision=found.get(r["id"]),
        )
        for r in rows
    ]
    return Queue(
        items=items,
        totals_by_state=totals,
        truncated=sum(totals.values()) > len(items),
    )
