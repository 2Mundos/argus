"""Read-only view of a task's independent review rounds (pre-merge independent review 3.7).

Each round is one `code_review` child, oldest first (round 1). Nothing here writes or
decides: spawning, picking, counting and completion stay in `independent_review`.
"""

import json
import re
from datetime import datetime
from typing import Any, Literal

import aiosqlite
from argus_core.tasks import TaskState
from pydantic import BaseModel

from argus_api.tasks.independent_review import CODE_REVIEW_KIND

Verdict = Literal["approve", "request_changes", "void"]
_PRIORITY = re.compile(r"^\s*\[?(P\d)\b\]?\s*[:\-]?\s*", re.IGNORECASE)
_ENDED = (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)


class ReviewFinding(BaseModel):
    priority: str | None = None
    file: str | None = None
    lines: str | None = None
    description: str


class ReviewRound(BaseModel):
    child_id: str
    round: int  # oldest = 1
    state: TaskState
    verdict: Verdict | None  # null while running or when the child ended without one
    head_sha: str | None
    findings: list[ReviewFinding]
    findings_by_priority: dict[str, int]
    reviewer_provider: str | None
    reviewer_model: str | None
    artifact_ids: list[str]
    created_at: datetime
    finished_at: datetime | None


class ReviewRounds(BaseModel):
    task_id: str
    rounds: list[ReviewRound]


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _finding(raw: Any) -> ReviewFinding:
    if isinstance(raw, dict):
        lines = raw.get("lines", raw.get("line"))
        if isinstance(lines, list):
            lines = "-".join(str(n) for n in lines[:2])
        text = raw.get("description") or raw.get("message") or raw.get("title")
        return ReviewFinding(
            priority=_text(raw.get("priority") or raw.get("severity")),
            file=_text(raw.get("file") or raw.get("path")),
            lines=_text(lines),
            description=str(text) if text else json.dumps(raw, ensure_ascii=False),
        )
    text = str(raw)
    hit = _PRIORITY.match(text)
    if hit is None:
        return ReviewFinding(description=text)
    return ReviewFinding(priority=hit.group(1).upper(), description=text[hit.end() :])


def _data(raw: str | None) -> dict[str, Any]:
    try:
        data = (json.loads(raw) if raw else {}).get("data")
    except (json.JSONDecodeError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


async def _voided(conn: aiosqlite.Connection, parent_id: str) -> set[str]:
    async with conn.execute(
        "SELECT data FROM events WHERE subject_id = ? AND kind = 'review.independent_void'",
        (parent_id,),
    ) as cur:
        rows = await cur.fetchall()
    ids: set[str] = set()
    for (raw,) in rows:
        try:
            ids.add(json.loads(raw).get("child_id"))
        except (json.JSONDecodeError, AttributeError):
            continue
    return ids


async def _by_child(
    conn: aiosqlite.Connection, sql: str, ids: list[str]
) -> dict[str, list[aiosqlite.Row]]:
    found: dict[str, list[aiosqlite.Row]] = {i: [] for i in ids}
    marks = ",".join("?" * len(ids))
    async with conn.execute(sql.format(marks=marks), ids) as cur:
        for row in await cur.fetchall():
            found[row[0]].append(row)
    return found


async def review_rounds(conn: aiosqlite.Connection, parent_id: str) -> ReviewRounds:
    async with conn.execute(
        "SELECT id, state, result, created_at, updated_at, input FROM tasks"
        " WHERE parent_id = ? AND kind = ? ORDER BY rowid",
        (parent_id, CODE_REVIEW_KIND),
    ) as cur:
        children = list(await cur.fetchall())
    if not children:
        return ReviewRounds(task_id=parent_id, rounds=[])
    ids = [c["id"] for c in children]
    voided = await _voided(conn, parent_id)
    runs = await _by_child(
        conn,
        "SELECT task_id, provider, observed_model, configured_model"
        " FROM model_executions WHERE task_id IN ({marks})"
        " ORDER BY recorded_at DESC, id DESC",
        ids,
    )
    artifacts = await _by_child(
        conn,
        "SELECT task_id, id FROM artifacts"
        " WHERE source = 'worker' AND task_id IN ({marks})"
        " ORDER BY attempt, created_at, id",
        ids,
    )
    rounds = []
    for n, child in enumerate(children, 1):
        data = _data(child["result"])
        ended = child["state"] in _ENDED
        verdict: Verdict | None = None
        if child["id"] in voided:
            verdict = "void"
        elif child["state"] == TaskState.SUCCEEDED and data.get("verdict") in (
            "approve",
            "request_changes",
        ):
            verdict = data["verdict"]
        findings = [_finding(f) for f in data.get("findings") or []]
        by_priority: dict[str, int] = {}
        for f in findings:
            key = f.priority or "unranked"
            by_priority[key] = by_priority.get(key, 0) + 1
        run = runs[child["id"]][0] if runs[child["id"]] else None
        try:
            asked = json.loads(child["input"] or "{}").get("head_sha")
        except json.JSONDecodeError:
            asked = None
        rounds.append(
            ReviewRound(
                child_id=child["id"],
                round=n,
                state=child["state"],
                verdict=verdict,
                head_sha=data.get("head_sha") or asked,
                findings=findings,
                findings_by_priority=by_priority,
                reviewer_provider=run["provider"] if run else None,
                reviewer_model=(run["observed_model"] or run["configured_model"])
                if run
                else None,
                artifact_ids=[a["id"] for a in artifacts[child["id"]]],
                created_at=child["created_at"],
                finished_at=child["updated_at"] if ended else None,
            )
        )
    return ReviewRounds(task_id=parent_id, rounds=rounds)


class ReviewBadge(BaseModel):
    """What a board card shows for a parent: the newest round and its verdict."""

    round: int
    verdict: Verdict | None
    state: TaskState


async def badges(
    conn: aiosqlite.Connection, parent_ids: list[str]
) -> dict[str, ReviewBadge]:
    """Newest-round badge for each parent that has review children (few queries)."""
    if not parent_ids:
        return {}
    marks = ",".join("?" * len(parent_ids))
    async with conn.execute(
        "SELECT parent_id, id, state, result FROM tasks"  # noqa: S608
        f" WHERE kind = ? AND parent_id IN ({marks}) ORDER BY rowid",
        [CODE_REVIEW_KIND, *parent_ids],
    ) as cur:
        rows = list(await cur.fetchall())
    count: dict[str, int] = {}
    newest: dict[str, aiosqlite.Row] = {}
    for row in rows:
        count[row["parent_id"]] = count.get(row["parent_id"], 0) + 1
        newest[row["parent_id"]] = row
    out = {}
    for parent, row in newest.items():
        data = _data(row["result"])
        verdict = (
            data.get("verdict")
            if row["state"] == TaskState.SUCCEEDED
            and data.get("verdict") in ("approve", "request_changes")
            else None
        )
        out[parent] = ReviewBadge(
            round=count[parent], verdict=verdict, state=row["state"]
        )
    return out
