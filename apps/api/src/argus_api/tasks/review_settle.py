"""An approved parent waiting on the forge settles when the forge shows it delivered
(settings spec 3.5).

A loop (never a GET) looks at `in_review` tasks blocked `awaiting_forge`: `review=none`,
acceptance off, independently approved, but the forge did not yet show the PR green or
merged on the approved head. It reads the forge outside any write, and the write
re-checks the task and the read before accepting. It never opens a manual review for a
parent that did not ask for one.
"""

import asyncio
import logging
import time

import aiosqlite
from argus_core.decisions import REVIEW_OPTIONS, Audience, DecisionKind
from argus_core.tasks import Review

from argus_api.db import Database
from argus_api.decisions import store as decisions
from argus_api.tasks import delivery, merge_resume, steward
from argus_api.tasks import independent_review as indep
from argus_api.tasks import service as tasks
from argus_api.tasks.deliverable import ForgeRead

log = logging.getLogger(__name__)

ACTOR = "system:review-settle"
MAX_PER_PASS = 10  # forge reads per pass
BACKOFF_S = (60.0, 900.0)  # first retry delay, cap (doubles per unchanged pass)


async def candidates(conn: aiosqlite.Connection) -> list[str]:
    async with conn.execute(
        "SELECT id FROM tasks WHERE state = 'in_review' AND archived_at IS NULL"
        " AND block_reason = ? ORDER BY updated_at, id LIMIT 200",
        (indep.AWAITING_FORGE,),
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def settle_task(db: Database, checker: delivery.PrChecker, task_id: str) -> bool:
    """Settle one waiting parent from a fresh forge read; True when it succeeded."""
    async with db.read() as conn:
        row = await tasks.task_row(conn, task_id)
        if row["block_reason"] != indep.AWAITING_FORGE:
            return False
        reviewed_pr, reviewed = await indep.reviewed_approval_of(conn, task_id)
        forge = None
        if await indep.awaits_settlement(conn, row):
            task = await tasks.get_task(conn, task_id)
            forge = ForgeRead(await delivery.delivery_of(conn, checker, task))
    async with db.write() as tx:
        row = await tasks.task_row(tx.conn, task_id)
        if row["state"] != "in_review" or row["block_reason"] != indep.AWAITING_FORGE:
            return False  # settled, cancelled or answered meanwhile
        if await indep.has_active_reviewer(tx.conn, task_id):
            return False
        if await decisions.open_ids(tx.conn, "task", task_id, (DecisionKind.REVIEW,)):
            return False
        if not await indep.settles_alone(tx.conn, row):
            # Manual review or acceptance was switched on while waiting: the regular
            # review decision applies instead.
            await tx.conn.execute(
                "UPDATE tasks SET block_reason = NULL WHERE id = ?", (task_id,)
            )
            await decisions.open_decision(
                tx,
                subject_type="task",
                subject_id=task_id,
                kind=DecisionKind.REVIEW,
                question=(
                    f"Review the result of {row['title']!r} (attempt {row['attempt']})."
                ),
                options=list(REVIEW_OPTIONS),
                audience=Audience.HUMAN
                if Review(row["review"]) is Review.HUMAN
                else Audience.ORCHESTRATOR,
                requested_by=ACTOR,
            )
            return False
        if (
            await indep.reviewed_approval_of(tx.conn, task_id)
            != (reviewed_pr, reviewed)
            or forge is None
        ):
            return False  # a newer approval arrived; the next pass reads again
        settled = await indep.settle_or_wait(
            tx, row, forge, reviewed, ACTOR, reviewed_pr=reviewed_pr
        )
        if not settled:
            # A closed or broken approved PR gets a repair or a decision (PR steward
            # spec 3.1); a ready one hands the merge to the worker path once
            # (pre-merge independent review spec 3.6).
            if await steward.step(
                tx,
                await tasks.task_row(tx.conn, task_id),
                forge,
                reviewed_pr,
                reviewed,
                ACTOR,
            ):
                return False
            await merge_resume.resume_merge(
                tx,
                await tasks.task_row(tx.conn, task_id),
                forge,
                reviewed_pr,
                reviewed,
                ACTOR,
            )
        return settled


async def sweep(
    db: Database, checker: delivery.PrChecker, next_at: dict[str, float]
) -> list[str]:
    """One bounded pass; returns the task ids that succeeded. A task still waiting
    backs off (in memory: a restart reads again, bounded by MAX_PER_PASS)."""
    async with db.read() as conn:
        waiting = await candidates(conn)
    now = time.monotonic()
    for gone in set(next_at) - set(waiting):
        del next_at[gone]
    settled = []
    for task_id in [t for t in waiting if next_at.get(t, 0.0) <= now][:MAX_PER_PASS]:
        if await settle_task(db, checker, task_id):
            settled.append(task_id)
        else:
            first, cap = BACKOFF_S
            prev = next_at.get(task_id, 0.0) - now
            next_at[task_id] = now + min(max(prev * 2, first), cap)
    return settled


async def run(db: Database, checker: delivery.PrChecker, interval_s: float) -> None:
    next_at: dict[str, float] = {}
    while True:
        await asyncio.sleep(interval_s)
        try:
            for task_id in await sweep(db, checker, next_at):
                log.info("review-settle: accepted task %s", task_id)
        except Exception:
            log.exception("review-settle pass failed; trying again next interval")
