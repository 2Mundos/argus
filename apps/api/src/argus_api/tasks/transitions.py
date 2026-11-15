"""The state-transition core: `_apply`, its budget and acceptance checks, and
dependency promotion.

See docs/specs/worker-lifecycle.spec.md 3.3.
"""

import json
from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.continuation import (
    ChecklistItem,
    Continuation,
    ItemStatus,
    merge_acceptance,
)
from argus_core.decisions import REVIEW_OPTIONS, Audience, DecisionKind
from argus_core.difficulty import Difficulty, DifficultySource, escalated
from argus_core.tasks import (
    TERMINAL,
    Action,
    Review,
    ReviewDisposition,
    TaskState,
    TransitionContext,
    transition,
)

from argus_api.accounts.usage import ensure_attempt_outcome
from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import ConflictError
from argus_api.events import record
from argus_api.routines import failures as routine_failures
from argus_api.tasks import continuation as cont
from argus_api.tasks import cost as task_cost
from argus_api.tasks import finalization, steers
from argus_api.tasks.cost import BUDGET_EXHAUSTED
from argus_api.tasks.reads import _TICK, S, _deps, _row, _states

# Reasons for which the next attempt moves one difficulty level up (task difficulty
# spec 3): the cheaper route could not hold the task.
ESCALATING_REASONS = frozenset({"context_overflow", "verification_failed"})


def _escalation(row: aiosqlite.Row, reason: str) -> dict[str, dict[str, Any]]:
    """The difficulty bump (columns and event data) a failed attempt earns, or {}."""
    current = Difficulty(row["difficulty"])
    target = escalated(current)
    if reason not in ESCALATING_REASONS or target is current:
        return {}
    return {
        "set": {"difficulty": target, "difficulty_source": DifficultySource.INFERRED},
        "data": {
            "difficulty_escalated": {"from": current, "to": target, "reason": reason}
        },
    }


async def _apply(
    tx: Tx,
    row: aiosqlite.Row,
    action: Action,
    actor: str,
    *,
    set_: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    deps_done: bool = True,
    counts_failure: bool = True,
    event: str | None = None,
    review: Review | None = None,
    open_review_decision: bool = True,
    exhausted: bool = False,
    disposition: tuple[str, str | None] | None = None,
    voided: bool = False,
) -> TaskState:
    """Validate and persist one transition, its event (`task.<action>` unless
    `event` names it), and its knock-on effects.

    `voided=True` ends a claim that did no work (a readiness deferral before any
    executor started): the attempt number is handed back, the attempt's snapshot row
    is dropped, and it leaves no usage placeholder, claim-end mark or interruption.

    `counts_failure=False` evaluates a fail/expire as if no failure had counted, so it
    can never exhaust the attempts (the caller leaves `failures` unchanged).
    `open_review_decision=False` enters `in_review` without a human/orchestrator
    decision (independent code review gate; settings.spec.md 3.5).
    `exhausted=True` evaluates a fail/expire as if no attempt were left (the
    automatic resume rule's fail with the cause, failure classes 3.2).
    `disposition` is the (outcome, reason) a success records; by default an accept
    is `accepted` and anything else `not_reviewed` (deliverable spec 3.10)."""
    old = S(row["state"])
    failures = row["failures"] if counts_failure else -1
    new = transition(
        old,
        action,
        _context(
            row,
            deps_done=deps_done,
            review=review,
            failures=failures,
            exhausted=exhausted,
        ),
    )
    # A requeue, expiry or release that would start another attempt checks the
    # budget first: when it is spent the task blocks instead (task cost spec 3.6).
    over_budget = (
        await task_cost.exhausted_reason(tx.conn, row)
        if old is S.CLAIMED and new is S.READY
        else None
    )
    if over_budget is not None:
        new = S.BLOCKED
    # strictly greater than the old version, so a transition always stales a pending edit
    bumped = ts(max(tx.now, datetime.fromisoformat(row["updated_at"]) + _TICK))
    fields: dict[str, Any] = {
        "state": new,
        "updated_at": bumped,
        "retry_at": None,  # only a transient fail's requeue waits (failure classes 3.2)
        **(set_ or {}),
    }
    if over_budget is not None:
        fields |= _budget_block(over_budget)
        data = {**(data or {}), "block_reason": BUDGET_EXHAUSTED}
    if new is S.SUCCEEDED:
        settled = await _settle_acceptance(tx, row, action, actor, disposition)
        fields["review_disposition"] = settled.model_dump_json()
        data = {**(data or {}), "disposition": settled.outcome}
        if settled.unmet:
            data["unmet"] = settled.unmet
    leaving_claim = old is S.CLAIMED and new is not S.CLAIMED
    voided = voided and leaving_claim
    if voided:
        fields["attempt"] = row["attempt"] - 1
        data = {**(data or {}), "voided": True}
    if leaving_claim:
        fields |= {
            "assignee_agent_id": None,
            "lease_expires_at": None,
            "timeout_at": None,
            "paused_disk_at": None,  # a disk pause belongs to one attempt
            "oom_kill": (set_ or {}).get("oom_kill", 0),
        }
    assignments = ", ".join(f"{k} = ?" for k in fields)
    await tx.conn.execute(
        f"UPDATE tasks SET {assignments} WHERE id = ?",  # noqa: S608 - keys are code-controlled
        (*fields.values(), row["id"]),
    )
    await record(
        tx,
        actor,
        "task",
        row["id"],
        event or f"task.{action}",
        {"from": old, "to": new, "attempt": row["attempt"], **(data or {})},
    )
    if leaving_claim:
        # Pending steers belong to this attempt only: they expire, or end `unknown`
        # when a heartbeat carried them (task steering spec 3.3).
        await steers.close_attempt(tx, row["id"], row["attempt"])
        if voided:
            await tx.conn.execute(
                "DELETE FROM task_attempts WHERE id = ("
                " SELECT max(id) FROM task_attempts WHERE task_id = ? AND attempt = ?)",
                (row["id"], row["attempt"]),
            )
        else:
            await finalization.record_claim_end(
                tx, row["id"], row["attempt"], str(action.value)
            )
            await _ensure_usage_outcome(tx, row, action, data or {})
    # Decisions that waited on the old state are moot once the task leaves it
    # (an answered decision is already closed before its transition runs).
    if (moot := moot_decisions(old, new)) is not None:
        await decisions.cancel_open(tx, "task", row["id"], moot[0])
    if old is S.CLAIMED and action in _INTERRUPTIONS and not voided:
        await cont.on_interruption(
            tx,
            row["id"],
            row["attempt"],
            _interruption_reason(action, data or {}),
            str((data or {}).get("error") or (data or {}).get("reason") or "") or None,
            actor,
        )
    if new is S.IN_REVIEW and open_review_decision:
        review = review or Review(row["review"])
        await decisions.open_decision(
            tx,
            subject_type="task",
            subject_id=row["id"],
            kind=DecisionKind.REVIEW,
            question=f"Review the result of {row['title']!r} (attempt {row['attempt']}).",
            options=list(REVIEW_OPTIONS),
            audience=Audience.HUMAN
            if review is Review.HUMAN
            else Audience.ORCHESTRATOR,
            requested_by=actor,
        )
    if new is S.SUCCEEDED:
        await _promote_dependents(tx, row["id"])
    elif new in (S.FAILED, S.CANCELLED):
        await _flag_dependents(tx, row["id"], new)
    if new in (S.SUCCEEDED, S.FAILED, S.CANCELLED) and row["origin_conversation_id"]:
        # Imported here: conversations import the orchestrator, which imports us.
        from argus_api.conversations import service as conversations

        await conversations.queue_task_update(tx, row["origin_conversation_id"])
    if new is S.FAILED and row["created_by"].startswith("routine:"):
        await routine_failures.after_failed_run(
            tx, row["created_by"].removeprefix("routine:")
        )
    elif new is S.SUCCEEDED and row["created_by"].startswith("routine:"):
        # Imported here: intake creates tasks, so it imports this module.
        from argus_api.routines import findings as routine_findings

        await routine_findings.after_succeeded_run(
            tx, row["created_by"].removeprefix("routine:"), row["id"]
        )
    return new


def _budget_block(message: str) -> dict[str, Any]:
    return {
        "block_reason": BUDGET_EXHAUSTED,
        "retry_at": None,
        "progress_message": message,
        "progress_percent": None,
        "progress_kind": None,
        "progress_data": None,
    }


def _context(
    row: aiosqlite.Row,
    *,
    deps_done: bool = True,
    review: Review | None = None,
    failures: int | None = None,
    exhausted: bool = False,
) -> TransitionContext:
    return TransitionContext(
        failures=row["max_attempts"]
        if exhausted
        else row["failures"]
        if failures is None
        else failures,
        max_attempts=row["max_attempts"],
        review=review or Review(row["review"]),
        deps_done=deps_done,
    )


def moot_decisions(
    old: TaskState, new: TaskState
) -> tuple[tuple[DecisionKind, ...] | None] | None:
    """Which open decisions the move `old` -> `new` cancels: None for none,
    `(None,)` for all of them, `(kinds,)` for those kinds."""
    if new in TERMINAL:
        return (None,)
    if old in decisions.WAITS_IN and new is not old:
        return (decisions.WAITS_IN[old],)
    return None


async def planned_state(
    conn: aiosqlite.Connection, task_id: str, action: Action
) -> tuple[aiosqlite.Row, TaskState]:
    """The task row and the state `action` would move it to, without writing.
    Raises InvalidTransitionError exactly as the real transition would."""
    row = await _row(conn, task_id)
    done = action is not Action.APPROVE or await _deps_done(conn, task_id)
    return row, transition(S(row["state"]), action, _context(row, deps_done=done))


def review_escalation(row: aiosqlite.Row) -> dict[str, Any] | None:
    """The difficulty bump a rejected review earns, as `review` records it."""
    return (
        _escalation(row, "verification_failed")
        .get("data", {})
        .get("difficulty_escalated")
    )


async def _deps_done(conn: aiosqlite.Connection, task_id: str) -> bool:
    states = await _states(conn, await _deps(conn, task_id))
    return all(s is S.SUCCEEDED for s in states.values())


def _interruption_reason(action: Action, data: dict[str, Any]) -> str:
    fixed = _INTERRUPTIONS[action]
    reason = str(data.get("reason") or action)
    if fixed:
        return fixed
    return "asked" if reason.startswith("waiting for decision") else reason


def _unmet(items: list[ChecklistItem]) -> list[str]:
    return [i.id for i in items if i.status is not ItemStatus.DONE]


def _held_items(row: aiosqlite.Row, have: Continuation) -> list[ChecklistItem]:
    return merge_acceptance(have.checklist, json.loads(row["acceptance"]))


def unmet_acceptance(row: aiosqlite.Row) -> list[str]:
    """The acceptance items a success would leave unproven (deliverable spec 3.10)."""
    return _unmet(_held_items(row, cont.load(row)))


def check_acceptance_met(row: aiosqlite.Row) -> None:
    """A plain accept never closes over unproven criteria (deliverable spec 3.10)."""
    if unmet := unmet_acceptance(row):
        raise ConflictError(
            f"task {row['id']} has acceptance items without evidence: {', '.join(unmet)};"
            " evidence them (update_task_continuation), reject, or answer"
            " accept_with_deviation with the deviation and who authorized it",
            code="acceptance_unmet",
            details=unmet,
        )


async def _settle_acceptance(
    tx: Tx,
    row: aiosqlite.Row,
    action: Action,
    actor: str,
    disposition: tuple[str, str | None] | None,
) -> ReviewDisposition:
    """How the task succeeds. Items not `done` stay visibly unproven: a pending one
    becomes `verification_missing`, never `done` (deliverable spec 3.10)."""
    have = await cont.current(tx, row["id"])
    items = _held_items(row, have)
    unmet = _unmet(items)
    if unmet:
        settled = [
            i.model_copy(
                update={"status": ItemStatus.VERIFICATION_MISSING, "updated_at": tx.now}
            )
            if i.status is ItemStatus.PENDING
            else i
            for i in items
        ]
        await cont.store(
            tx,
            row["id"],
            have,
            have.model_copy(update={"checklist": settled}),
            actor,
            "unmet_at_success",
        )
    outcome, reason = disposition or (
        ("accepted", None) if action is Action.ACCEPT else ("not_reviewed", None)
    )
    return ReviewDisposition.model_validate(
        {
            "outcome": outcome,
            "reason": reason,
            "unmet": unmet,
            "decided_by": actor,
            "decided_at": tx.now,
            "attempt": row["attempt"],
        }
    )


# Actions that end a claim without a result: what the next attempt must be told.
# None: the reason is in the transition's data.
_INTERRUPTIONS: dict[Action, str | None] = {
    Action.FAIL: None,
    Action.EXPIRE: None,
    Action.RELEASE: "released",
    Action.REQUEUE: "quota",
    Action.BLOCK: None,
}


async def _promote_dependents(tx: Tx, task_id: str) -> None:
    async with tx.conn.execute(
        "SELECT t.* FROM tasks t JOIN task_deps d ON d.task_id = t.id"
        " WHERE d.depends_on_id = ? AND t.state = 'pending' AND NOT EXISTS ("
        "  SELECT 1 FROM task_deps d2 JOIN tasks u ON u.id = d2.depends_on_id"
        "  WHERE d2.task_id = t.id AND u.state != 'succeeded')"
        " ORDER BY t.id",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await _apply(tx, row, Action.DEPS_SATISFIED, "system")


async def _flag_dependents(tx: Tx, task_id: str, state: TaskState) -> None:
    """Dependents of a failed/cancelled task stay put; the orchestrator decides."""
    async with tx.conn.execute(
        "SELECT t.id FROM tasks t JOIN task_deps d ON d.task_id = t.id"
        " WHERE d.depends_on_id = ? AND t.state IN ('pending', 'awaiting_approval')"
        " ORDER BY t.id",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await _dependency_failed_event(tx, row["id"], task_id, state)


async def _dependency_failed_event(
    tx: Tx, task_id: str, dependency_id: str, state: TaskState
) -> None:
    await record(
        tx,
        "system",
        "task",
        task_id,
        "task.dependency_failed",
        {"dependency_id": dependency_id, "dependency_state": state},
    )


async def _ensure_usage_outcome(
    tx: Tx, row: aiosqlite.Row, action: Action, data: dict[str, Any]
) -> None:
    """Every leave-claimed write leaves a usage outcome (account usage 3.10).

    When no `task_attempts` row exists, claim id 0 is used (deterministic run_key
    `a1:<task>:<attempt>:0`); 0 means no recorded claim.
    """
    async with tx.conn.execute(
        "SELECT id, claimed_at, agent_id FROM task_attempts"
        " WHERE task_id = ? AND attempt = ? ORDER BY id DESC LIMIT 1",
        (row["id"], row["attempt"]),
    ) as cur:
        claim = await cur.fetchone()
    await ensure_attempt_outcome(
        tx,
        task_id=row["id"],
        attempt=row["attempt"],
        agent_id=row["assignee_agent_id"] or (claim["agent_id"] if claim else None),
        claim_id=claim["id"] if claim else 0,
        claimed_at=claim["claimed_at"] if claim else None,
        reason=_usage_outcome_reason(action, data),
    )


def _usage_outcome_reason(action: Action, data: dict[str, Any]) -> str:
    if action is Action.RELEASE:
        return "released"
    if action is Action.REQUEUE:
        return "requeued"
    if action is Action.CANCEL:
        return "cancelled"
    if action is Action.EXPIRE:
        why = str(data.get("reason") or "")
        if why in ("timeout", "stalled"):
            return why
        return "lost"  # lease_expired, agent_ended, and unknown expiry
    return "no_report"  # complete, fail, block
