"""Review report of one project (review finding verification spec 3.5).

Read-only operator view: how independent-review findings fared over the last days.
The `findings` section is the evidence for moving `verify_findings` from `observe`
to `enforce`: refuted findings that a later round raised again estimate how often a
verifier would have dropped a real problem.
"""

import json
from datetime import timedelta
from typing import Annotated, Any

import aiosqlite
from argus_core.tasks import TaskState
from fastapi import APIRouter, Query
from pydantic import BaseModel

from argus_api.db import ts
from argus_api.deps import AdminDep, DbDep
from argus_api.knowledge import service as knowledge
from argus_api.tasks import review_policy
from argus_api.tasks.independent_review import (
    CODE_REVIEW_KIND,
    RESPONDED_EVENT,
    VERIFIED_EVENT,
)

router = APIRouter(tags=["tasks"])
MAX_DAYS = 90


class DisputeOutcomes(BaseModel):
    """What the next round did with each finding the implementer disputed."""

    disputed: int = 0
    dropped: int = 0
    raised_again: int = 0
    no_next_round: int = 0


class TaskRepeats(BaseModel):
    task_id: str
    repeats: int


class RefutedOutcomes(BaseModel):
    refuted: int = 0
    raised_again_later: int = 0  # estimates real problems a verifier would have dropped


class FindingsReport(BaseModel):
    by_priority: dict[str, int] = {}
    by_verification: dict[str, int] = {}  # confirmed, refuted, unclear, unverified
    by_priority_verification: dict[str, dict[str, int]] = {}
    responses: dict[str, int] = {}  # fixed, disputed, unanswered, pending
    disputes: DisputeOutcomes = DisputeOutcomes()
    repeats: int = 0
    repeats_by_task: list[TaskRepeats] = []
    refuted: RefutedOutcomes = RefutedOutcomes()


class ReviewReport(BaseModel):
    project: str
    days: int
    findings: FindingsReport
    policy: review_policy.PolicyReport


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _findings_of(raw: str | None) -> list[dict[str, Any]]:
    try:
        data = (json.loads(raw) if raw else {}).get("data") or {}
    except (json.JSONDecodeError, AttributeError):
        return []
    return [
        f
        for f in data.get("findings") or []
        if isinstance(f, dict) and isinstance(f.get("id"), str)
    ]


async def _events(
    conn: aiosqlite.Connection, project: str, kind: str
) -> list[tuple[str, dict[str, Any]]]:
    async with conn.execute(
        "SELECT e.subject_id, e.data FROM events e JOIN tasks t ON t.id = e.subject_id"
        " WHERE e.subject_type = 'task' AND e.kind = ? AND t.project = ? ORDER BY e.id",
        (kind, project),
    ) as cur:
        return [(r[0], json.loads(r[1] or "{}")) for r in await cur.fetchall()]


async def findings_report(
    conn: aiosqlite.Connection, project: str, since: str
) -> FindingsReport:
    async with conn.execute(
        "SELECT id, parent_id, result FROM tasks WHERE kind = ? AND project = ?"
        " AND state = ? AND parent_id IS NOT NULL ORDER BY rowid",
        (CODE_REVIEW_KIND, project, TaskState.SUCCEEDED),
    ) as cur:
        children = list(await cur.fetchall())
    # Later rounds of a task are read whole, so a repeat is found even when the
    # window starts between the two rounds.
    in_window = {r["id"] for r in await _created_since(conn, project, since)}
    verified = {
        (d.get("review_child_id"), d.get("id")): d.get("status")
        for _, d in await _events(conn, project, VERIFIED_EVENT)
    }
    responses = {
        d.get("child_id"): d.get("responses") or {}
        for _, d in await _events(conn, project, RESPONDED_EVENT)
    }
    rounds: dict[str, list[tuple[str, list[dict[str, Any]]]]] = {}
    for c in children:
        rounds.setdefault(c["parent_id"], []).append(
            (c["id"], _findings_of(c["result"]))
        )
    report = FindingsReport()
    repeats_by_task: dict[str, int] = {}
    for parent_id, task_rounds in rounds.items():
        for n, (child_id, findings) in enumerate(task_rounds):
            later = task_rounds[n + 1 :]
            raised = {
                f["repeat_of"]
                for _, fs in later
                for f in fs
                if isinstance(f.get("repeat_of"), str)
            }
            if child_id not in in_window:
                continue
            for f in findings:
                key = f"{child_id}:{f['id']}"
                _bump(report.by_priority, str(f.get("priority") or "unranked"))
                status = verified.get((child_id, f["id"])) or "unverified"
                _bump(report.by_verification, status)
                _bump(
                    report.by_priority_verification.setdefault(
                        str(f.get("priority") or "unranked"), {}
                    ),
                    status,
                )
                answer = (responses.get(child_id) or {}).get(f["id"])
                _bump(report.responses, answer["status"] if answer else "pending")
                if f.get("repeat_of"):
                    report.repeats += 1
                    repeats_by_task[parent_id] = repeats_by_task.get(parent_id, 0) + 1
                if answer and answer["status"] == "disputed":
                    report.disputes.disputed += 1
                    if not later:
                        report.disputes.no_next_round += 1
                    elif key in raised:
                        report.disputes.raised_again += 1
                    else:
                        report.disputes.dropped += 1
                if status == "refuted":
                    report.refuted.refuted += 1
                    if key in raised:
                        report.refuted.raised_again_later += 1
    report.repeats_by_task = [
        TaskRepeats(task_id=t, repeats=n) for t, n in sorted(repeats_by_task.items())
    ]
    return report


async def _created_since(
    conn: aiosqlite.Connection, project: str, since: str
) -> list[aiosqlite.Row]:
    async with conn.execute(
        "SELECT id FROM tasks WHERE kind = ? AND project = ? AND created_at >= ?",
        (CODE_REVIEW_KIND, project, since),
    ) as cur:
        return list(await cur.fetchall())


@router.get("/projects/{slug}/review-report")
async def get_review_report(
    slug: str,
    db: DbDep,
    _: AdminDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30,
) -> ReviewReport:
    """How a project's independent-review findings and `review.policy` tiers fared in
    the last `days` days (default 30, at most 90). Admin only; read-only."""
    async with db.snapshot() as snap:
        await knowledge.get_project(snap.conn, slug)  # 404 for an unknown project
        since = ts(snap.now - timedelta(days=days))
        return ReviewReport(
            project=slug,
            days=days,
            findings=await findings_report(snap.conn, slug, since),
            policy=await review_policy.policy_report(snap.conn, slug, since, days),
        )
