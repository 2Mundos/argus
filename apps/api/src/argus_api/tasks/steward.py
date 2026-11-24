"""PR steward: an approved PR that cannot merge gets a repair or a decision
(docs/specs/pr-steward.spec.md).

`review_settle` calls `step` with the forge read it already took. A closed PR, or an
open one whose checks failed or that no longer merges, either requeues one bounded
worker repair (`review.independent.max_repairs`) or opens a decision for a person.
Argus never writes to the forge: the worker repairs and pushes through `argus-task`.
"""

import json
from datetime import timedelta
from typing import Any

import aiosqlite
from argus_core.decisions import (
    PR_NOT_READY_GATE,
    REVIEW_OPTIONS,
    Audience,
    DecisionKind,
)
from argus_core.delivery import CheckState, PrState
from argus_core.tasks import FORGE_DELIVERABLES, Action, Deliverable, TaskState

from argus_api.db import Tx
from argus_api.decisions import store as decisions
from argus_api.events import record
from argus_api.settings import service as settings_svc
from argus_api.tasks import deliverable, readiness
from argus_api.tasks import independent_review as indep

REQUESTED_EVENT = "review.steward_repair_requested"
PUSHED_EVENT = "review.steward_repair_pushed"
STOPPED_EVENT = "review.steward_stopped"
STEWARD_EVENTS = (REQUESTED_EVENT, PUSHED_EVENT, STOPPED_EVENT)
MAX_FAILED_CHECKS = 20
_BROKEN = (CheckState.FAILURE, CheckState.ERROR, CheckState.CANCELLED)


def classify(
    seen: Any,
    forge_policy: readiness.ForgeReview,
    now: Any,
    reviewed_pr: str | None,
    reviewed_head: str | None,
) -> tuple[list[str], list[str]] | None:
    """(classes, failed check names) when the fresh, complete read shows the approved
    PR closed or broken; None to keep waiting or settle as today (3.1)."""
    if (
        seen is None
        or not reviewed_head
        or seen.stale
        or seen.partial
        or now - seen.checked_at > timedelta(seconds=indep.FORGE_FRESH_S)
        or not indep._same_pr(seen.pr, reviewed_pr)
    ):
        return None
    if seen.pr_state is PrState.CLOSED:
        return ["pr_closed"], []
    if (
        seen.pr_state is not PrState.OPEN
        or seen.head is None
        or not indep._same_sha(seen.head.sha, reviewed_head)
    ):
        return None
    reasons = readiness.pr_gate(seen, forge_policy).reasons
    classes = [c for c in ("ci_failed", "not_mergeable") if c in reasons]
    failed = [c.name for c in seen.checks if c.state in _BROKEN][:MAX_FAILED_CHECKS]
    return (classes, failed) if classes else None


async def _ids(conn: aiosqlite.Connection, task_id: str, kind: str) -> list[int]:
    async with conn.execute(
        "SELECT id FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ? ORDER BY id",
        (task_id, kind),
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def repairs_requested(conn: aiosqlite.Connection, task_id: str) -> int:
    return len(await _ids(conn, task_id, REQUESTED_EVENT))


async def repaired_for_approval(conn: aiosqlite.Connection, task_id: str) -> bool:
    """True when a repair was requested after the newest approval."""
    passed = await _ids(conn, task_id, indep.PASSED_EVENT)
    asked = await _ids(conn, task_id, REQUESTED_EVENT)
    return bool(passed and asked and asked[-1] > passed[-1])


async def pending_repair(
    conn: aiosqlite.Connection, task_id: str
) -> dict[str, Any] | None:
    """Data of the newest repair request whose outcome is not recorded yet."""
    async with conn.execute(
        "SELECT id, kind, data FROM events WHERE subject_type = 'task'"
        " AND subject_id = ? AND kind IN (?, ?) ORDER BY id DESC LIMIT 1",
        (task_id, REQUESTED_EVENT, PUSHED_EVENT),
    ) as cur:
        row = await cur.fetchone()
    if row is None or row[1] != REQUESTED_EVENT:
        return None
    data = json.loads(row[2]) if row[2] else {}
    return data if isinstance(data, dict) else {}


async def record_pushed(
    tx: Tx, task_id: str, pr: str | None, new_head: str | None, actor: str
) -> None:
    """A repair attempt completed on a moved head: note it; the caller reviews the new
    head from the start. A repair is not a review round."""
    asked = await pending_repair(tx.conn, task_id)
    old = asked.get("head_sha") if asked else None
    if not old or not new_head or indep._same_sha(old, new_head):
        return
    await record(
        tx,
        actor,
        "task",
        task_id,
        PUSHED_EVENT,
        {"pr": pr, "old_head_sha": old, "new_head_sha": new_head.lower()},
    )


def _repair_note(
    attempt: int, pr: str, head: str, classes: list[str], failed: list[str]
) -> str:
    lines = [f"## PR repair (attempt {attempt})", ""]
    lines.append(f"Independent review approved {pr} at {head}, but it cannot merge:")
    if "ci_failed" in classes:
        names = ", ".join(f"`{n}`" for n in failed) or "a check"
        lines.append(f"- failed checks: {names}")
    if "not_mergeable" in classes:
        lines.append("- the branch conflicts with its base and does not merge cleanly")
    lines += [
        "",
        "Change only what makes those checks pass or the branch merge cleanly; add no"
        " scope.",
        f"Read failures with `argus-task ci-digest --sha {head}`. For a conflict,"
        " rebase onto the base and resolve it.",
        'Push through `argus-task deliver --undo-note "<reason>"` so the PR keeps a'
        " note on how to undo the push.",
        "When the failure is not caused by the change (for example an unrelated flaky"
        " check), change nothing and say so in the completion.",
    ]
    return "\n".join(lines)


async def _stop(
    tx: Tx,
    parent: aiosqlite.Row,
    cls: str,
    pr: str,
    head: str | None,
    failed: list[str],
    actor: str,
) -> None:
    await tx.conn.execute(
        "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent["id"],)
    )
    names = f" Failed checks: {', '.join(failed)}." if failed else ""
    what = {
        "pr_closed": "was closed without merging",
        "ci_failed": "has failing checks",
        "not_mergeable": "does not merge cleanly into its base",
    }[cls]
    decision_id = await decisions.open_decision(
        tx,
        subject_type="task",
        subject_id=parent["id"],
        kind=DecisionKind.REVIEW,
        question=(
            f"{parent['title']!r}: the approved pull request {pr} {what} ({cls}).{names}"
            " Fix or merge it on the forge, then accept; or reject to send the task"
            " back."
        ),
        options=list(REVIEW_OPTIONS),
        audience=Audience.HUMAN,
        requested_by=actor,
        payload={
            "gate": PR_NOT_READY_GATE,
            "class": cls,
            "pr": pr,
            "head_sha": head,
            "failed_checks": failed,
        },
    )
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        STOPPED_EVENT,
        {"class": cls, "pr": pr, "head_sha": head, "decision_id": decision_id},
    )


async def step(
    tx: Tx,
    parent: aiosqlite.Row,
    forge: deliverable.ForgeRead | None,
    reviewed_pr: str | None,
    reviewed_head: str | None,
    actor: str,
) -> bool:
    """Classify the read and repair or stop; true when the parent was requeued or a
    decision opened. Runs in the caller's write transaction; every gate is read again
    here, so a stale read or wakeup does nothing."""
    from argus_api.tasks import service as tasks_svc

    task_id = parent["id"]
    if (
        parent["state"] != TaskState.IN_REVIEW
        or parent["block_reason"] != indep.AWAITING_FORGE
        or parent["kind"] == indep.CODE_REVIEW_KIND
        or Deliverable(parent["deliverable"]) not in FORGE_DELIVERABLES
        or await indep.has_active_reviewer(tx.conn, task_id)
        or await decisions.open_ids(tx.conn, "task", task_id, (DecisionKind.REVIEW,))
    ):
        return False
    setting, _ = await indep._load_settings(tx.conn, parent["project"])
    if setting.mode == "off":
        return False
    seen = forge.delivery if forge is not None else None
    forge_policy = readiness.forge_review_policy(
        await settings_svc.forge_agentic_review_required(tx.conn, parent["project"])
    )
    found = classify(seen, forge_policy, tx.now, reviewed_pr, reviewed_head)
    if seen is None or found is None:
        return False
    classes, failed = found
    head = seen.head.sha.lower() if seen.head and seen.head.sha else None
    if classes == ["pr_closed"]:
        await _stop(tx, parent, "pr_closed", seen.pr, head, [], actor)
        return True
    if (
        setting.max_repairs > 0
        and not await repaired_for_approval(tx.conn, task_id)
        and await repairs_requested(tx.conn, task_id) < setting.max_repairs
    ):
        note = _repair_note(parent["attempt"] + 1, seen.pr, head or "", classes, failed)
        await tasks_svc._apply(
            tx,
            parent,
            Action.REJECT,
            actor,
            set_={"spec": f"{parent['spec']}\n\n{note}", "block_reason": None},
            data={"steward_repair": True},
            open_review_decision=False,
        )
        await record(
            tx,
            actor,
            "task",
            task_id,
            REQUESTED_EVENT,
            {
                "pr": seen.pr,
                "head_sha": head,
                "classes": classes,
                "failed_checks": failed,
            },
        )
        return True
    await _stop(tx, parent, classes[0], seen.pr, head, failed, actor)
    return True
