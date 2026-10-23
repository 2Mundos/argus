"""Persistence of the task continuation contract (docs/specs/task-continuation.spec.md).

One JSON column, fenced by its own `revision`. Every write is inside the caller's
transaction and makes no network call: runtimes observe the live system first, then
report here. Callers (tasks.service, decisions.service) pass rows; this module never
imports them.
"""

from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ChecklistItem,
    ChecklistReport,
    Continuation,
    ContinuationArtifact,
    ContinuationInterruption,
    ContinuationProvenance,
    ContinuationReconciliation,
    EffectiveDecision,
    EnvCheck,
    ItemStatus,
    classify_answer,
    clean,
)

from argus_api.db import Tx
from argus_api.errors import ConflictError
from argus_api.events import record


def load(row: aiosqlite.Row) -> Continuation:
    raw = row["continuation"]
    return Continuation.model_validate_json(raw) if raw else Continuation()


async def current(tx: Tx, task_id: str) -> Continuation:
    async with tx.conn.execute(
        "SELECT continuation FROM tasks WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    return (
        Continuation.model_validate_json(row[0]) if row and row[0] else Continuation()
    )


async def store(
    tx: Tx,
    task_id: str,
    before: Continuation,
    after: Continuation,
    actor: str,
    why: str,
    *,
    audit: bool = True,
) -> Continuation:
    """Write `after` with `revision` + 1 when it differs from `before`, and record it
    (`audit=False`: the caller's own event already says why). `updated_at` stays: the
    task version fences edits, the revision fences this."""
    if after == before:
        return before
    after = after.model_copy(update={"revision": before.revision + 1})
    await tx.conn.execute(
        "UPDATE tasks SET continuation = ? WHERE id = ?",
        (after.model_dump_json(exclude_none=True), task_id),
    )
    if audit:
        await record(
            tx,
            actor,
            "task",
            task_id,
            "task.continuation_updated",
            {"revision": after.revision, "why": why},
        )
    return after


def fence(have: Continuation, expected: int | None) -> None:
    if expected is not None and expected != have.revision:
        raise ConflictError(
            f"continuation is at revision {have.revision}, not {expected}",
            code="stale_continuation",
        )


def upsert_artifact(
    items: list[ContinuationArtifact], new: ContinuationArtifact
) -> list[ContinuationArtifact]:
    """Replace the artifact with the same (kind, ref) unless an observation newer than
    `new` already stands; keep known facts a thinner report does not carry."""
    out: list[ContinuationArtifact] = []
    placed = False
    for a in items:
        if a.key != new.key:
            out.append(a)
            continue
        placed = True
        if new.observed_at < a.observed_at:
            out.append(a)
            continue
        out.append(
            new.model_copy(
                update={
                    "url": new.url or a.url,
                    "head_sha": new.head_sha or a.head_sha,
                    "state": a.state
                    if new.state is ArtifactState.UNKNOWN
                    else new.state,
                }
            )
        )
    if not placed:
        out.append(new)
    return out[-20:]


def reconcile(
    c: Continuation, observed: list[ContinuationArtifact], at: datetime
) -> list[ContinuationReconciliation]:
    """Contradictions between what is recorded and what was just seen. A state moving
    forward (open -> merged) is progress, not a contradiction; a moved head or a merge
    that is no longer a merge is."""
    known = {a.key: a for a in c.artifacts}
    found = {r.subject: r for r in c.reconciliation}
    seen_ok: set[str] = set()
    for obs in observed:
        was = known.get(obs.key)
        subject = f"{obs.kind.value} {obs.ref}"
        if was is None or obs.state is ArtifactState.UNKNOWN:
            continue
        moved = bool(was.head_sha and obs.head_sha and was.head_sha != obs.head_sha)
        unmerged = (
            was.state is ArtifactState.MERGED and obs.state is not ArtifactState.MERGED
        )
        if moved or unmerged:
            found[subject] = ContinuationReconciliation(
                subject=subject,
                recorded=f"{was.state.value} {was.head_sha or ''}".strip(),
                observed=f"{obs.state.value} {obs.head_sha or ''}".strip(),
                at=at,
            )
        else:
            seen_ok.add(subject)
    return [r for s, r in found.items() if s not in seen_ok][-10:]


async def on_milestone(
    tx: Tx,
    row: aiosqlite.Row,
    attempt: int,
    kind: str,
    data: dict[str, Any],
    actor: str,
) -> None:
    """Persist a structured progress milestone as a known artifact. The milestone
    itself stays per attempt on the task row; this is what survives it."""
    c = load(row)
    new: list[ContinuationArtifact] = []
    if kind == "commit_created":
        new = [
            ContinuationArtifact(
                kind=ArtifactKind.BRANCH,
                ref=data["branch"],
                state=ArtifactState.UNKNOWN,
                provenance=ContinuationProvenance.WORKER,
                observed_at=tx.now,
                attempt=attempt,
            ),
            ContinuationArtifact(
                kind=ArtifactKind.COMMIT,
                ref=data["sha"],
                head_sha=data["sha"],
                state=ArtifactState.UNKNOWN,  # committed; pushed is not claimed
                provenance=ContinuationProvenance.WORKER,
                observed_at=tx.now,
                attempt=attempt,
                note=f"on {data['branch']}"[:300],
            ),
        ]
    elif kind == "pr_opened":
        new = [
            ContinuationArtifact(
                kind=ArtifactKind.PR,
                ref=str(data["number"]),
                url=data["url"],
                state=ArtifactState.OPEN,
                provenance=ContinuationProvenance.WORKER,
                observed_at=tx.now,
                attempt=attempt,
            )
        ]
    if not new:
        return
    artifacts = c.artifacts
    for a in new:
        artifacts = upsert_artifact(artifacts, a)
    await store(
        tx, row["id"], c, c.model_copy(update={"artifacts": artifacts}), actor, kind
    )


async def on_interruption(
    tx: Tx, task_id: str, attempt: int, reason: str, detail: str | None, actor: str
) -> None:
    c = await current(tx, task_id)
    cut = ContinuationInterruption(
        attempt=attempt, reason=reason[:100], detail=clean(detail, 500), at=tx.now
    )
    # the transition's own event (task.fail, task.release, ...) is the audit trail
    await store(
        tx,
        task_id,
        c,
        c.model_copy(update={"interruption": cut}),
        actor,
        reason,
        audit=False,
    )


async def on_decision(
    tx: Tx,
    task_id: str,
    *,
    decision_id: str,
    question: str,
    choice: str | None,
    text: str,
    options: list[str] | None,
    category: str,
    actor: str,
    attempt: int,
) -> None:
    """Record an answered question as an effective decision with its scope and time.
    A newer answer to the same question supersedes the older one; nothing is deleted
    from the spec narrative or the decisions table (audit)."""
    c = await current(tx, task_id)
    answer = " — ".join(x for x in (choice, text) if x)
    new = EffectiveDecision(
        decision_id=decision_id,
        question=clean(question, 1000) or "",
        answer=clean(answer, 2000) or "",
        choice=choice,
        options_known=bool(options),
        category="assistance" if category == "assistance" else "decision",
        status=classify_answer(choice, text, options),
        decided_by=actor,
        decided_at=tx.now,
        attempt=attempt,
    )
    same = _norm(new.question)
    kept = [
        d.model_copy(update={"status": "superseded"})
        if _norm(d.question) == same and d.category == new.category
        else d
        for d in c.decisions
    ]
    decisions = [*kept, new][-30:]
    await store(
        tx, task_id, c, c.model_copy(update={"decisions": decisions}), actor, "decision"
    )


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


async def answered_equivalent(
    tx: Tx, task_id: str, question: str
) -> EffectiveDecision | None:
    """An effective goal decision on this same question, so asking again is not needed.
    Assistance answers never settle a question: a missing tool may be missing again."""
    want = _norm(question)
    for d in reversed((await current(tx, task_id)).decisions):
        if (
            d.category == "decision"
            and d.status == "effective"
            and _norm(d.question) == want
        ):
            return d
    return None


def apply_reports(
    items: list[ChecklistItem],
    reports: list[ChecklistReport],
    provenance: ContinuationProvenance,
    at: datetime,
) -> list[ChecklistItem]:
    """Worker claims per item. `done` without evidence is `verification_missing`;
    an id Argus does not hold is ignored (a report cannot add criteria)."""
    by_id = {r.id: r for r in reports}
    out = []
    for item in items:
        rep = by_id.get(item.id)
        if rep is None:
            out.append(item)
            continue
        evidence = clean(rep.evidence, 2000)
        status = rep.status
        if status is ItemStatus.DONE and not (evidence and evidence.strip()):
            status = ItemStatus.VERIFICATION_MISSING
        out.append(
            item.model_copy(
                update={
                    "status": status,
                    "evidence": evidence,
                    "provenance": provenance,
                    "updated_at": at,
                }
            )
        )
    return out


def apply_environment(
    have: list[EnvCheck], new: list[EnvCheck], at: datetime
) -> list[EnvCheck]:
    by = {e.resource: e for e in have}
    for e in new:
        by[e.resource] = e.model_copy(update={"observed_at": at})
    return list(by.values())[-20:]
