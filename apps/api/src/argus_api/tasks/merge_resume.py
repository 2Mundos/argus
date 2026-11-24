"""Resume the authorized worker merge after an exact-head independent approval
(pre-merge independent review spec 3.6).

An approved `pr_merged` parent waits `awaiting_forge`. When the forge shows its PR open
and green on exactly the approved head and the task's own merge grants allow it, the
parent is requeued once per approval for a worker attempt: that worker holds the
per-attempt merge authorization scoped to the approved PR and head and runs
`argus-task merge`, the only forge writer. Argus never merges itself.
"""

import json
from datetime import timedelta
from typing import Any

import aiosqlite
from argus_core.decisions import REVIEW_OPTIONS, Audience, DecisionKind
from argus_core.delivery import PrState
from argus_core.tasks import Action, Deliverable, TaskState

from argus_api.db import Tx
from argus_api.decisions import store as decisions
from argus_api.events import record
from argus_api.settings import service as settings_svc
from argus_api.tasks import deliverable, readiness, review_policy
from argus_api.tasks import independent_review as indep

RESUMED_EVENT = "review.merge_resumed"
BLOCKED_EVENT = "review.merge_resume_blocked"


async def _newest_id(
    conn: aiosqlite.Connection, task_id: str, kind: str
) -> tuple[int, dict[str, Any]]:
    async with conn.execute(
        "SELECT id, data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0, {}
    data = json.loads(row[1]) if row[1] else {}
    return row[0], data if isinstance(data, dict) else {}


async def resumed_for_approval(
    conn: aiosqlite.Connection, task_id: str, pr: str | None, head: str | None
) -> bool:
    """True when the current approval of exactly (pr, head) already earned its one
    merge attempt: a resume event newer than the approval, for the same PR and head."""
    passed = 0
    for kind in (indep.PASSED_EVENT, review_policy.SKIPPED_EVENT):
        passed = max(passed, (await _newest_id(conn, task_id, kind))[0])
    resumed, data = await _newest_id(conn, task_id, RESUMED_EVENT)
    return (
        resumed > passed > 0
        and indep._same_pr(data.get("pr"), pr)
        and indep._same_sha(data.get("head_sha"), head)
    )


async def _block(tx: Tx, parent_id: str, reason: str, actor: str, **extra: Any) -> None:
    _, last = await _newest_id(tx.conn, parent_id, BLOCKED_EVENT)
    if last.get("reason") != reason or last.get("head_sha") != extra.get("head_sha"):
        await record(
            tx, actor, "task", parent_id, BLOCKED_EVENT, {"reason": reason, **extra}
        )


async def resume_merge(
    tx: Tx,
    parent: aiosqlite.Row,
    forge: deliverable.ForgeRead | None,
    reviewed_pr: str | None,
    reviewed_head: str | None,
    actor: str,
) -> bool:
    """Requeue an approved, green, open PR for the authorized worker merge. Runs in the
    caller's write transaction with a forge read taken just before it; true when the
    parent was requeued. Every gate is re-read here, so a stale wakeup does nothing."""
    from argus_api.tasks import service as tasks_svc

    task_id = parent["id"]
    if (
        parent["state"] != TaskState.IN_REVIEW
        or parent["block_reason"] != indep.AWAITING_FORGE
        or parent["kind"] == indep.CODE_REVIEW_KIND
        or Deliverable(parent["deliverable"]) is not Deliverable.PR_MERGED
        or await indep.has_active_reviewer(tx.conn, task_id)
        or await decisions.open_ids(tx.conn, "task", task_id, (DecisionKind.REVIEW,))
    ):
        return False
    seen = forge.delivery if forge is not None else None
    head = seen.head if seen is not None else None
    if seen is None or head is None:
        return False
    if (
        seen.pr_state is not PrState.OPEN
        or seen.stale
        or tx.now - seen.checked_at > timedelta(seconds=indep.FORGE_FRESH_S)
        or not reviewed_head
        or not indep._same_pr(seen.pr, reviewed_pr)
        or not indep._same_sha(head.sha, reviewed_head)
    ):
        return False  # moved, replaced, closed, merged or unread: settle decides
    if await review_policy.human_gate(tx.conn, parent, reviewed_pr, reviewed_head):
        return False  # a person merges this head (review policy spec 3.4)
    if await resumed_for_approval(tx.conn, task_id, reviewed_pr, reviewed_head):
        return False  # one attempt per approval; a retry is an explicit decision
    forge_policy = readiness.forge_review_policy(
        await settings_svc.forge_agentic_review_required(tx.conn, parent["project"])
    )
    if not readiness.pr_gate(seen, forge_policy).ready:
        return False  # CI, review or mergeability not met on this head yet
    allowed, why = await indep.merge_gate_status(
        tx.conn, parent, pr=seen.pr, head_sha=head.sha
    )
    if not allowed:
        await _block(tx, task_id, why or "independent_review_required", actor)
        return False
    task = await tasks_svc.get_task(tx.conn, task_id)
    decision = readiness.decide_merge(
        task,
        await settings_svc.auto_merge_setting(tx.conn, task.project),
        independent_review_required=True,
        independent_review_approved=True,
    )
    if not decision.may_merge:
        await _block(
            tx,
            task_id,
            "merge_not_authorized"
            if not decision.authorized
            else "repository_write_missing",
            actor,
            head_sha=head.sha,
        )
        return False
    note = (
        f"## Merge continuation (attempt {parent['attempt'] + 1})\n\n"
        f"Independent review approved {seen.pr} at {head.sha}. Change no code."
        f" Run `argus-task merge --pr <number> --sha {head.sha}` and report the"
        " merged result; if the head moved or a gate fails, report that instead."
    )
    await tasks_svc._apply(
        tx,
        parent,
        Action.REJECT,
        actor,
        set_={"spec": f"{parent['spec']}\n\n{note}", "block_reason": None},
        data={"merge_continuation": True},
        open_review_decision=False,
    )
    await record(
        tx,
        actor,
        "task",
        task_id,
        RESUMED_EVENT,
        {"pr": seen.pr, "head_sha": reviewed_head.lower()},
    )
    return True


async def open_unmerged_decision(
    tx: Tx, parent: aiosqlite.Row, forge: deliverable.ForgeRead | None, actor: str
) -> None:
    """The merge attempt ended and the forge still shows the PR open: ask for a
    decision instead of looping or merging by another path."""
    seen = forge.delivery if forge is not None else None
    if seen is None or seen.pr_state is not PrState.OPEN:
        return  # merged (settle accepts), closed or unread: the sweeper decides
    await decisions.open_decision(
        tx,
        subject_type="task",
        subject_id=parent["id"],
        kind=DecisionKind.REVIEW,
        question=(
            f"The authorized merge attempt for {parent['title']!r} ended without"
            f" merging {seen.pr}. Merge it, then accept; or reject to try again."
        ),
        options=list(REVIEW_OPTIONS),
        audience=Audience.ORCHESTRATOR,
        requested_by=actor,
    )
