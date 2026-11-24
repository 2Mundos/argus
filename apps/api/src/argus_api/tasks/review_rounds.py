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

from argus_api.tasks.independent_review import (
    CODE_REVIEW_KIND,
    VERIFICATION_KIND,
    VERIFIED_EVENT,
    responses_by_child,
)

Verdict = Literal["approve", "request_changes", "void"]
_PRIORITY = re.compile(r"^\s*\[?(P\d)\b\]?\s*[:\-]?\s*", re.IGNORECASE)
_ENDED = (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)


ResponseStatus = Literal["fixed", "disputed", "unanswered"]
VerificationStatus = Literal["confirmed", "refuted", "unclear"]


class ReviewFinding(BaseModel):
    id: str | None = None  # null for a finding stored before findings were structured
    priority: str | None = None
    file: str | None = None
    lines: str | None = None
    title: str | None = None
    description: str
    evidence_kind: Literal["test", "reproduction", "reading"] | None = None
    repeat_of: str | None = None
    # How the implementer answered this finding; null until the next attempt completes.
    response: ResponseStatus | None = None
    response_evidence: str | None = None
    # For a repeat: how the earlier round's finding was answered.
    repeat_response: ResponseStatus | None = None
    repeat_round: int | None = None
    verification: VerificationStatus | None = None


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
    repeats: int  # findings raised again from an earlier round
    verifier_child_id: str | None
    verifier_state: TaskState | None
    artifact_ids: list[str]
    created_at: datetime
    finished_at: datetime | None


class StewardState(BaseModel):
    """The newest PR steward event (PR steward spec 3.5): a repair requested, a repair
    pushed, or the steward stopped with a decision."""

    state: Literal["repair_requested", "repair_pushed", "stopped"]
    classes: list[str] = []
    failed_checks: list[str] = []
    pr: str | None = None
    head_sha: str | None = None
    old_head_sha: str | None = None
    new_head_sha: str | None = None
    decision_id: str | None = None
    at: datetime


class ReviewRounds(BaseModel):
    task_id: str
    repeats: int = 0  # findings raised again, over all rounds
    rounds: list[ReviewRound]
    steward: StewardState | None = None


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _finding(raw: Any) -> ReviewFinding:
    if isinstance(raw, dict):
        lines = raw.get("lines", raw.get("line"))
        if isinstance(lines, list):
            lines = "-".join(str(n) for n in lines[:2])
        text = raw.get("description") or raw.get("message") or raw.get("title")
        evidence = raw.get("evidence")
        kind = evidence.get("kind") if isinstance(evidence, dict) else None
        return ReviewFinding(
            id=_text(raw.get("id")),
            priority=_text(raw.get("priority") or raw.get("severity")),
            file=_text(raw.get("file") or raw.get("path")),
            lines=_text(lines),
            title=_text(raw.get("title")),
            description=str(text) if text else json.dumps(raw, ensure_ascii=False),
            evidence_kind=kind if kind in ("test", "reproduction", "reading") else None,
            repeat_of=_text(raw.get("repeat_of")),
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


def _reviewed_head(child: aiosqlite.Row) -> str | None:
    """The head a round reviewed: its verdict's `head_sha`, else the one it was asked for."""
    try:
        asked = json.loads(child["input"] or "{}").get("head_sha")
    except (json.JSONDecodeError, AttributeError):
        asked = None
    return _data(child["result"]).get("head_sha") or asked


async def _voided(
    conn: aiosqlite.Connection, parent_id: str, children: list[aiosqlite.Row]
) -> set[str]:
    """Children whose outcome a `review.independent_void` event invalidated.

    Most events name their `child_id`; `head_moved` / `pr_moved` (from `settle_or_wait`)
    do not, so they void the rounds that reviewed the `reviewed` head, or else the newest
    round that existed when they were recorded."""
    async with conn.execute(
        "SELECT at, data FROM events WHERE subject_id = ? AND kind = 'review.independent_void'"
        " ORDER BY id",
        (parent_id,),
    ) as cur:
        rows = await cur.fetchall()
    ids: set[str] = set()
    for at, raw in rows:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("child_id"):
            ids.add(data["child_id"])
        elif data.get("reason") in ("head_moved", "pr_moved"):
            reviewed = data.get("reviewed")
            hit = [
                c["id"]
                for c in children
                if reviewed
                and (_reviewed_head(c) or "").strip().lower()
                == str(reviewed).strip().lower()
            ]
            if not hit:
                before = [c["id"] for c in children if c["created_at"] <= at]
                hit = before[-1:]
            ids.update(hit)
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


_STEWARD_STATES = {
    "review.steward_repair_requested": "repair_requested",
    "review.steward_repair_pushed": "repair_pushed",
    "review.steward_stopped": "stopped",
}


async def _steward(conn: aiosqlite.Connection, parent_id: str) -> StewardState | None:
    marks = ",".join("?" * len(_STEWARD_STATES))
    async with conn.execute(
        f"SELECT kind, data, at FROM events WHERE subject_type = 'task'"  # noqa: S608
        f" AND subject_id = ? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
        (parent_id, *_STEWARD_STATES),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    data = json.loads(row["data"]) if row["data"] else {}
    classes = data.get("classes") or ([data["class"]] if data.get("class") else [])
    return StewardState(
        state=_STEWARD_STATES[row["kind"]],  # ty: ignore[invalid-argument-type]
        classes=classes,
        failed_checks=data.get("failed_checks") or [],
        pr=data.get("pr"),
        head_sha=data.get("head_sha"),
        old_head_sha=data.get("old_head_sha"),
        new_head_sha=data.get("new_head_sha"),
        decision_id=data.get("decision_id"),
        at=row["at"],
    )


async def _verifications(
    conn: aiosqlite.Connection, parent_id: str
) -> tuple[dict[str, aiosqlite.Row], dict[tuple[str, str], VerificationStatus]]:
    """(review child id -> its newest verifier child, (review child id, finding id) ->
    verification status)."""
    verifiers: dict[str, aiosqlite.Row] = {}
    async with conn.execute(
        "SELECT id, state, input FROM tasks WHERE parent_id = ? AND kind = ?"
        " ORDER BY rowid",
        (parent_id, VERIFICATION_KIND),
    ) as cur:
        for row in await cur.fetchall():
            try:
                review_id = json.loads(row["input"] or "{}").get("review_child_id")
            except json.JSONDecodeError:
                continue
            if isinstance(review_id, str):
                verifiers[review_id] = row
    verified: dict[tuple[str, str], VerificationStatus] = {}
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ? ORDER BY id",
        (parent_id, VERIFIED_EVENT),
    ) as cur:
        for (raw,) in await cur.fetchall():
            data = json.loads(raw or "{}")
            verified[(data.get("review_child_id"), data.get("id"))] = data.get("status")
    return verifiers, verified


def _annotate(
    f: ReviewFinding,
    child_id: str,
    responses: dict[str, dict[str, dict[str, Any]]],
    verified: dict[tuple[str, str], VerificationStatus],
    numbers: dict[str, int],
) -> None:
    """Fill in the implementer's response, the verification and the repeat's history."""
    if f.id is not None:
        got = responses.get(child_id, {}).get(f.id)
        if got:
            f.response = got["status"]
            f.response_evidence = got.get("evidence") or None
        f.verification = verified.get((child_id, f.id))
    if f.repeat_of:
        earlier, _, fid = f.repeat_of.partition(":")
        f.repeat_round = numbers.get(earlier)
        f.repeat_response = (
            responses.get(earlier, {}).get(fid, {}).get("status", "unanswered")
        )


async def review_rounds(conn: aiosqlite.Connection, parent_id: str) -> ReviewRounds:
    steward = await _steward(conn, parent_id)
    async with conn.execute(
        "SELECT id, state, result, created_at, updated_at, input FROM tasks"
        " WHERE parent_id = ? AND kind = ? ORDER BY rowid",
        (parent_id, CODE_REVIEW_KIND),
    ) as cur:
        children = list(await cur.fetchall())
    if not children:
        return ReviewRounds(task_id=parent_id, rounds=[], steward=steward)
    ids = [c["id"] for c in children]
    voided = await _voided(conn, parent_id, children)
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
    responses = await responses_by_child(conn, parent_id)
    verifiers, verified = await _verifications(conn, parent_id)
    numbers = {c["id"]: n for n, c in enumerate(children, 1)}
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
        for f in findings:
            _annotate(f, child["id"], responses, verified, numbers)
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
                repeats=sum(1 for f in findings if f.repeat_of),
                verifier_child_id=verifiers[child["id"]]["id"]
                if child["id"] in verifiers
                else None,
                verifier_state=verifiers[child["id"]]["state"]
                if child["id"] in verifiers
                else None,
                artifact_ids=[a["id"] for a in artifacts[child["id"]]],
                created_at=child["created_at"],
                finished_at=child["updated_at"] if ended else None,
            )
        )
    return ReviewRounds(
        task_id=parent_id,
        repeats=sum(r.repeats for r in rounds),
        rounds=rounds,
        steward=steward,
    )


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
        "SELECT parent_id, id, state, result, created_at, input FROM tasks"  # noqa: S608
        f" WHERE kind = ? AND parent_id IN ({marks}) ORDER BY rowid",
        [CODE_REVIEW_KIND, *parent_ids],
    ) as cur:
        rows = list(await cur.fetchall())
    voided = {
        p: await _voided(conn, p, [r for r in rows if r["parent_id"] == p])
        for p in {r["parent_id"] for r in rows}
    }
    count: dict[str, int] = {}
    newest: dict[str, aiosqlite.Row] = {}
    for row in rows:
        count[row["parent_id"]] = count.get(row["parent_id"], 0) + 1
        newest[row["parent_id"]] = row
    out = {}
    for parent, row in newest.items():
        data = _data(row["result"])
        verdict: Verdict | None = None
        if row["id"] in voided[parent]:
            verdict = "void"
        elif row["state"] == TaskState.SUCCEEDED and data.get("verdict") in (
            "approve",
            "request_changes",
        ):
            verdict = data["verdict"]
        out[parent] = ReviewBadge(
            round=count[parent], verdict=verdict, state=row["state"]
        )
    return out
