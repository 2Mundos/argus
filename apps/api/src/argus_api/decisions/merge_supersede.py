"""A merge that happened elsewhere closes the merge-assistance question that blocks its
task, and the task resumes to verify (task delivery spec 3.7).

Bounded and explicit: a loop (never a GET) looks at blocked tasks whose only open
question is a worker's `pr-merge` assistance request, reads the forge for the task's
one unambiguous PR outside any write, and when the forge shows it merged resolves
that question through the ordinary answer path. The same write records the merged PR
and a `close_evidence` objective, so the next claim starts from the existing PR. It
never completes or accepts the task, ticks a checklist item or waives a gate.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

import aiosqlite
from argus_core.auth import Scope
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ContinuationArtifact,
    ContinuationPhase,
    ContinuationProvenance,
    clean,
    effective,
)
from argus_core.decisions import (
    MERGE_ASSISTANCE_RESOURCE,
    Audience,
    DecisionAnswer,
    DecisionKind,
)
from argus_core.delivery import PrState
from argus_core.tasks import Task

from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.decisions import service as decisions
from argus_api.decisions import store
from argus_api.errors import ConflictError
from argus_api.events import record
from argus_api.tasks import continuation as cont
from argus_api.tasks import delivery
from argus_api.tasks import service as tasks

log = logging.getLogger(__name__)

ACTOR = "system:merge-supersede"
SUPERSEDED = "task.merge_blocker_superseded"
SKIPPED = "task.merge_blocker_skipped"
MAX_PER_PASS = 10  # forge reads per pass
MAX_CANDIDATES = 200  # rows looked at per pass
FRESH_S = 120  # how old the forge read may be when the write checks it
BACKOFF_S = (60.0, 900.0)  # first retry delay, cap (doubles per unchanged pass)


@dataclass(frozen=True)
class _System(Identity):
    """The reconciler answering as itself, with an orchestrator's authority only over
    a question the checks below proved obsolete."""

    @property
    def actor(self) -> str:
        return ACTOR


SYSTEM = _System("system", Scope.ORCHESTRATOR, None, "merge-supersede")


class Skip(Exception):
    """Why a task stays as it is. `audit` reasons are recorded once on the task;
    the others (the forge cannot say yet, the PR is not merged yet) are routine."""

    def __init__(
        self,
        reason: str,
        decision_id: str | None = None,
        *,
        audit: bool = True,
        **details: str | None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision_id = decision_id
        self.audit = audit
        self.details = details


@dataclass(frozen=True)
class Outcome:
    task_id: str
    resumed: bool
    reason: str  # "superseded", or why the task was left as it is


@dataclass
class Backoff:
    """Per-task retry delay, in memory: a restart starts over, which is bounded by
    MAX_PER_PASS reads per pass."""

    next_at: dict[str, float] = field(default_factory=dict)
    delay: dict[str, float] = field(default_factory=dict)

    def due(self, task_id: str, now: float) -> bool:
        return self.next_at.get(task_id, 0.0) <= now

    def failed(self, task_id: str, now: float) -> None:
        first, cap = BACKOFF_S
        delay = min(self.delay.get(task_id, first / 2) * 2, cap)
        self.delay[task_id] = delay
        self.next_at[task_id] = now + delay

    def keep(self, task_ids: set[str]) -> None:
        for known in (self.next_at, self.delay):
            for gone in set(known) - task_ids:
                del known[gone]


@dataclass(frozen=True)
class _Plan:
    row: aiosqlite.Row
    task: Task
    decision_id: str
    question: str
    ref: delivery.PrRef


def _resource(payload: dict | None) -> str:
    return str((payload or {}).get("resource") or "").strip().lower()


_ASSISTANCE = (
    "SELECT DISTINCT t.id FROM tasks t JOIN decisions d"
    " ON d.subject_type = 'task' AND d.subject_id = t.id"
    " WHERE t.state = 'blocked' AND t.archived_at IS NULL AND d.state = 'open'"
    " AND d.kind = 'question'"
    " AND json_extract(d.payload, '$.category') = 'assistance'"
)
_RESOURCE = " AND lower(trim(coalesce(json_extract(d.payload, '$.resource'), ''))) "
_NOT_AUDITED = (
    " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.subject_type = 'task'"
    " AND e.subject_id = t.id AND e.kind = ?"
    " AND json_extract(e.data, '$.decision_id') = d.id)"
)


async def candidates(conn: aiosqlite.Connection) -> tuple[list[str], list[str]]:
    """(typed, other): blocked tasks whose open question is `pr-merge` assistance, and
    those whose assistance question is something else and has not been explained yet.
    Bounded; the first are read from the forge, the second only get a note."""
    async with conn.execute(
        f"{_ASSISTANCE}{_RESOURCE}= ? ORDER BY t.updated_at, t.id LIMIT ?",
        (MERGE_ASSISTANCE_RESOURCE, MAX_CANDIDATES),
    ) as cur:
        typed = [r[0] for r in await cur.fetchall()]
    async with conn.execute(
        f"{_ASSISTANCE}{_RESOURCE}!= ?{_NOT_AUDITED} ORDER BY t.updated_at, t.id LIMIT ?",
        (MERGE_ASSISTANCE_RESOURCE, SKIPPED, MAX_CANDIDATES),
    ) as cur:
        other = [r[0] for r in await cur.fetchall()]
    return typed, other


async def _plan(conn: aiosqlite.Connection, task_id: str) -> _Plan:
    """Everything Argus holds that makes this block a superseded merge request, or the
    reason it is not. Run before the forge read and again inside the write."""
    row = await tasks.task_row(conn, task_id)
    if row["state"] != "blocked" or row["archived_at"] is not None:
        raise Skip("not_blocked", audit=False)
    open_ids = await store.open_ids(conn, "task", task_id, (DecisionKind.QUESTION,))
    if not open_ids:
        raise Skip("no_open_question", audit=False)
    found = [await store.get(conn, i) for i in open_ids]
    mine = [d for d in found if (d.payload or {}).get("category") == "assistance"]
    first = (mine or found)[0]
    if len(found) > 1:
        raise Skip("several_open_questions", first.id, count=str(len(found)))
    resource = _resource(first.payload)
    if not mine or resource != MERGE_ASSISTANCE_RESOURCE:
        raise Skip("not_merge_assistance", first.id, resource=resource or None)
    if first.audience is not Audience.ORCHESTRATOR or not first.requested_by.startswith(
        "agent:"
    ):
        raise Skip("not_a_worker_request", first.id)
    if row["block_reason"] != f"waiting for decision {first.id}":
        raise Skip("other_blocker", first.id, block_reason=row["block_reason"])
    proven = (await delivery.repositories(conn)).get(row["project"])
    if proven is None:
        raise Skip("no_repository", first.id)
    task = await tasks.get_task(conn, task_id)
    ref = await delivery.reference(conn, task)
    if ref is None:
        raise Skip("pr_unresolved", first.id)
    if (ref.host, ref.owner, ref.repo) != proven:
        raise Skip("wrong_repository", first.id, url=ref.url)
    # The extraction takes a bare number as the project's own PR and the newest of
    # several; here every recorded PR must be this one, in this repository.
    artifacts = task.continuation.artifacts if task.continuation else []
    prs = [a for a in artifacts if a.kind is ArtifactKind.PR]
    if len({a.ref for a in prs}) > 1:
        raise Skip("several_prs", first.id, url=ref.url)
    for a in prs:
        if a.url and delivery.url_ref(a.url, proven) != ref:
            raise Skip("wrong_repository", first.id, url=a.url)
    return _Plan(row, task, first.id, first.question, ref)


def _verdict(plan: _Plan, snap: delivery.ForgeSnapshot, now) -> None:
    if snap.pr_state is PrState.UNKNOWN:
        raise Skip("forge_unknown", plan.decision_id, audit=False)
    if snap.pr_state is not PrState.MERGED:
        raise Skip(
            "not_merged", plan.decision_id, audit=False, pr_state=snap.pr_state.value
        )
    if not (snap.head_sha and snap.merge_sha and snap.merged_at):
        raise Skip("evidence_incomplete", plan.decision_id, url=plan.ref.url)
    if now - snap.checked_at > timedelta(seconds=FRESH_S):
        raise Skip("stale_observation", plan.decision_id, audit=False)
    reported = delivery.task_reported_head(plan.task)
    if reported and not (
        snap.head_sha.startswith(reported) or reported.startswith(snap.head_sha)
    ):
        raise Skip(
            "head_moved",
            plan.decision_id,
            reported_head=reported,
            forge_head=snap.head_sha,
        )


def _objective(plan: _Plan, snap: delivery.ForgeSnapshot) -> str:
    return (
        f"Your merge request is obsolete: {plan.ref.url} was merged outside this task"
        f" (head {snap.head_sha}, merge commit {snap.merge_sha}, observed on the"
        f" forge {snap.checked_at:%Y-%m-%dT%H:%M:%SZ}). Do not merge again, open"
        " another PR, reimplement or revert newer base changes. Check every checklist"
        " item against the merged result and COMPLETE with per-item evidence. The"
        " merge does not show that pr-ready, CI or review passed: report what you"
        " could not verify as pending or verification_missing, with any parser or"
        " acceptance discrepancy."
    )[:1000]


async def reconcile_task(
    db: Database, checker: delivery.PrChecker, task_id: str
) -> Outcome:
    """Resolve this task's obsolete merge-assistance question, or leave it. Every
    check runs again inside the write, so of concurrent passes, answers and claims
    exactly one effect happens and the others find the question closed."""
    async with db.read() as conn:
        try:
            plan = await _plan(conn, task_id)
        except Skip as skip:
            return await _left(db, task_id, skip)
        snap = await checker.snapshot(conn, plan.ref)
    try:
        async with db.write() as tx:
            now_plan = await _plan(tx.conn, task_id)
            if now_plan.ref != plan.ref or now_plan.decision_id != plan.decision_id:
                raise Skip("pr_changed", plan.decision_id, audit=False)
            _verdict(now_plan, snap, tx.now)
            await _supersede(tx, now_plan, snap)
    except Skip as skip:
        return await _left(db, task_id, skip)
    except ConflictError as exc:
        if exc.code == "decision_not_open":  # answered first, by someone else
            return Outcome(task_id, False, "no_open_question")
        raise
    return Outcome(task_id, True, "superseded")


async def _supersede(tx, plan: _Plan, snap: delivery.ForgeSnapshot) -> None:
    row = plan.row
    task_id = row["id"]
    have = cont.load(row)
    pr = ContinuationArtifact(
        kind=ArtifactKind.PR,
        ref=str(plan.ref.number),
        url=plan.ref.url,
        head_sha=snap.head_sha,
        state=ArtifactState.MERGED,
        provenance=ContinuationProvenance.ARGUS,
        observed_at=snap.checked_at,
        note=clean(
            f"merged on the forge at {snap.merged_at}, merge commit {snap.merge_sha},"
            f" base {snap.base_ref}; observed by Argus, not by the worker",
        ),
    )
    artifacts = cont.upsert_artifact(have.artifacts, pr)
    kept = next(a for a in artifacts if a.key == pr.key)
    if kept.state is not ArtifactState.MERGED or kept.head_sha != snap.head_sha:
        raise Skip("record_conflict", plan.decision_id, url=plan.ref.url)
    want = have.model_copy(
        update={
            "artifacts": artifacts,
            "phase": ContinuationPhase.CLOSE_EVIDENCE,
            "objective": _objective(plan, snap),
        }
    )
    stored = await cont.store(
        tx, task_id, have, want, ACTOR, "merge_superseded", audit=False
    )
    pending = effective(stored, plan.task.acceptance).pending
    await record(
        tx,
        ACTOR,
        "task",
        task_id,
        SUPERSEDED,
        {
            "decision_id": plan.decision_id,
            "source": "forge",
            "repository": f"https://{plan.ref.host}/{plan.ref.owner}/{plan.ref.repo}",
            "number": plan.ref.number,
            "url": plan.ref.url,
            "head_sha": snap.head_sha,
            "head_ref": snap.head_ref,
            "merge_sha": snap.merge_sha,
            "merged_at": snap.merged_at.isoformat() if snap.merged_at else None,
            "checked_at": snap.checked_at.isoformat(),
            "pending_checklist": pending,
            "not_asserted": ["checks", "review", "acceptance", "completion"],
            "revision": stored.revision,
        },
    )
    await decisions.answer(
        tx,
        plan.decision_id,
        DecisionAnswer(
            text=(
                f"Superseded by Argus: {plan.ref.url} was merged on the forge (head"
                f" {snap.head_sha}, merge commit {snap.merge_sha}), so the merge"
                " request is obsolete. This is not completion or acceptance and does"
                " not show that checks or review passed; verify the checklist."
            )
        ),
        SYSTEM,
    )


async def _left(db: Database, task_id: str, skip: Skip) -> Outcome:
    """Leave the task as it is; say why once (a reason is recorded again only when it
    changes), so a task that stays blocked explains itself on its own timeline."""
    if skip.audit and skip.decision_id:
        async with db.write() as tx:
            async with tx.conn.execute(
                "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
                " AND kind = ? ORDER BY id DESC LIMIT 1",
                (task_id, SKIPPED),
            ) as cur:
                last = await cur.fetchone()
            seen = json.loads(last[0]) if last else {}
            if (seen.get("decision_id"), seen.get("reason")) != (
                skip.decision_id,
                skip.reason,
            ):
                await record(
                    tx,
                    ACTOR,
                    "task",
                    task_id,
                    SKIPPED,
                    {
                        "decision_id": skip.decision_id,
                        "reason": skip.reason,
                        **{k: v for k, v in skip.details.items() if v is not None},
                    },
                )
    return Outcome(task_id, False, skip.reason)


async def sweep(
    db: Database, checker: delivery.PrChecker, backoff: Backoff
) -> list[Outcome]:
    """One bounded pass: at most MAX_PER_PASS forge reads, tasks that did not change
    last time waiting out their backoff."""
    async with db.read() as conn:
        typed, other = await candidates(conn)
    now = time.monotonic()
    backoff.keep(set(typed))
    outcomes = []
    for task_id in [t for t in typed if backoff.due(t, now)][:MAX_PER_PASS]:
        outcome = await reconcile_task(db, checker, task_id)
        if outcome.resumed:
            backoff.keep(set(typed) - {task_id})
        else:
            backoff.failed(task_id, now)
        outcomes.append(outcome)
    outcomes += [await reconcile_task(db, checker, t) for t in other[:MAX_PER_PASS]]
    return outcomes


async def run(db: Database, checker: delivery.PrChecker, interval_s: float) -> None:
    backoff = Backoff()
    while True:
        await asyncio.sleep(interval_s)
        try:
            for outcome in await sweep(db, checker, backoff):
                if outcome.resumed:
                    log.info("merge-supersede: resumed task %s", outcome.task_id)
        except Exception:
            log.exception("merge-supersede pass failed; trying again next interval")
