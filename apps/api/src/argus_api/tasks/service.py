"""Task operations. Every write takes an open `Tx` and records its events in it.

See docs/specs/worker-lifecycle.spec.md 3.2 and 3.3.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.agents import LIVE, AgentState
from argus_core.auth import Scope
from argus_core.decisions import Audience, DecisionKind
from argus_core.kinds import TaskKind
from argus_core.tasks import (
    TERMINAL,
    Action,
    Review,
    Task,
    TaskCreate,
    TaskResult,
    TaskState,
    TransitionContext,
    transition,
)

from argus_api.accounts.usage import insert_usage
from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import (
    ConflictError,
    ForbiddenError,
    LeaseConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.kinds import service as kinds

log = logging.getLogger(__name__)
S = TaskState
_JSON_FIELDS = ("acceptance", "input", "required_capabilities")


# -- reads -------------------------------------------------------------------


async def get_task(conn: aiosqlite.Connection, task_id: str) -> Task:
    row = await _row(conn, task_id)
    return await _to_task(conn, row)


# -- creation ----------------------------------------------------------------


async def create_task(
    tx: Tx,
    body: TaskCreate,
    actor: str,
    *,
    as_worker: str | None = None,
    hourly_limit: int | None = None,
) -> Task:
    """Create a task.

    `as_worker` (an agent id) limits creation to subtasks of a task that agent
    currently holds (3.6). `hourly_limit` is the policy gate (3.10): past that
    many tasks by `actor` in the last hour, the task waits for human approval
    instead of being refused.
    """
    if body.parent_id is not None:
        parent = await _row(tx.conn, body.parent_id)
        if as_worker is not None and not (
            parent["state"] == S.CLAIMED
            and parent["assignee_agent_id"] == as_worker
            and parent["lease_expires_at"] > ts(tx.now)
        ):
            raise ForbiddenError(
                "workers may only create subtasks of a task they currently hold"
            )
    elif as_worker is not None:
        raise ForbiddenError("workers may only create subtasks (set parent_id)")
    body = _apply_kind_defaults(body, await kinds.get_kind(tx.conn, body.kind))
    if body.project is None:
        if body.parent_id is None:
            raise ValidationFailedError(
                "project is required (use 'global' for fleet-wide work)",
                code="project_required",
            )
        body = body.model_copy(update={"project": parent["project"]})
    await _check_project_and_pins(tx.conn, body)
    deps = sorted(set(body.depends_on))
    dep_states = await _states(tx.conn, deps)
    if missing := set(deps) - dep_states.keys():
        raise NotFoundError(f"dependency not found: {', '.join(sorted(missing))}")
    deps_done = all(s is S.SUCCEEDED for s in dep_states.values())
    over_limit = hourly_limit is not None and (
        await _created_last_hour(tx, actor) >= hourly_limit
    )
    if body.requires_approval or over_limit:
        state = S.AWAITING_APPROVAL
    else:
        state = S.READY if deps_done else S.PENDING

    task_id = str(uuid4())
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO tasks (id, parent_id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " lease_ttl_s, max_attempts, project, timeout_s, created_by, created_at,"
        " updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            body.parent_id,
            body.kind,
            body.title,
            body.spec,
            json.dumps(body.acceptance),
            json.dumps(body.input),
            json.dumps(sorted(set(body.required_capabilities))),
            body.priority,
            state,
            int(body.requires_approval),
            body.review,
            body.lease_ttl_s,
            body.max_attempts,
            body.project,
            body.timeout_s,
            actor,
            now,
            now,
        ),
    )
    await tx.conn.executemany(
        "INSERT INTO task_knowledge (task_id, page_id) VALUES (?, ?)",
        [(task_id, p) for p in dict.fromkeys(body.knowledge)],
    )
    await tx.conn.executemany(
        "INSERT INTO task_deps (task_id, depends_on_id) VALUES (?, ?)",
        [(task_id, d) for d in deps],
    )
    await record(tx, actor, "task", task_id, "task.created", {"to": state})
    if over_limit:
        await decisions.open_decision(
            tx,
            subject_type="task",
            subject_id=task_id,
            kind=DecisionKind.POLICY,
            question=(
                f"{actor} has created {hourly_limit} or more tasks in the last hour"
                f" (the policy limit). Allow {body.title!r} to proceed?"
            ),
            options=list(_APPROVAL_OPTIONS),
            audience=Audience.HUMAN,
            requested_by="system",
        )
    elif body.requires_approval:
        await decisions.open_decision(
            tx,
            subject_type="task",
            subject_id=task_id,
            kind=DecisionKind.APPROVAL,
            question=f"Approve starting {body.title!r}?",
            options=list(_APPROVAL_OPTIONS),
            audience=Audience.HUMAN,
            requested_by=actor,
        )
    for dep, dep_state in dep_states.items():
        if dep_state in (S.FAILED, S.CANCELLED):
            await _dependency_failed_event(tx, task_id, dep, dep_state)
    return await get_task(tx.conn, task_id)


# -- worker operations (require a valid lease) --------------------------------


async def claim(
    tx: Tx, agent_id: str, capabilities: list[str], *, default_timeout_s: int = 0
) -> Task | None:
    """Claim the highest-priority, oldest ready task the agent is capable of.

    The attempt's wall-clock deadline is the task's `timeout_s` (which creation
    filled from its kind), else `default_timeout_s`; 0 means no deadline.
    """
    state = await _agent_state(tx.conn, agent_id)
    if state is not AgentState.ACTIVE:
        raise ConflictError(
            f"agent {agent_id} is {state}; it may not claim new tasks",
            code=f"agent_{state}",
        )
    async with tx.conn.execute(
        "SELECT * FROM tasks t WHERE state = 'ready' AND NOT EXISTS ("
        "  SELECT 1 FROM json_each(t.required_capabilities) rc"
        "  WHERE rc.value NOT IN (SELECT value FROM json_each(?)))"
        " ORDER BY priority DESC, created_at, id LIMIT 1",
        (json.dumps(capabilities),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    attempt = row["attempt"] + 1
    await _apply(
        tx,
        row,
        Action.CLAIM,
        f"agent:{agent_id}",
        set_={
            "assignee_agent_id": agent_id,
            "attempt": attempt,
            "lease_expires_at": _deadline(tx.now, row["lease_ttl_s"]),
            "timeout_at": (
                _deadline(tx.now, timeout)
                if (timeout := row["timeout_s"] or default_timeout_s) > 0
                else None
            ),
        },
        data={"attempt": attempt, "agent_id": agent_id},
    )
    return await get_task(tx.conn, row["id"])


@dataclass(frozen=True)
class RenewedLease:
    task_id: str
    attempt: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class RejectedLease:
    task_id: str
    attempt: int
    code: str
    message: str


async def renew(
    tx: Tx, agent_id: str, leases: list[tuple[str, int]]
) -> tuple[list[RenewedLease], list[RejectedLease]]:
    """Extend each still-valid lease; a rejected pair never blocks the others."""
    renewed: list[RenewedLease] = []
    rejected: list[RejectedLease] = []
    for task_id, attempt in leases:
        try:
            row = await _owned(tx, task_id, attempt, agent_id)
        except (LeaseConflictError, NotFoundError) as exc:
            rejected.append(RejectedLease(task_id, attempt, exc.code, exc.message))
            continue
        deadline = tx.now + timedelta(seconds=row["lease_ttl_s"])
        await tx.conn.execute(
            "UPDATE tasks SET lease_expires_at = ? WHERE id = ?",
            (ts(deadline), task_id),
        )
        renewed.append(RenewedLease(task_id, attempt, deadline))
    return renewed, rejected


async def complete(
    tx: Tx, agent_id: str, task_id: str, attempt: int, result: TaskResult
) -> Task:
    """Complete with the result envelope; `data` must match the kind's result
    schema and every listed artifact must belong to this task (3.11)."""
    row = await _owned(tx, task_id, attempt, agent_id)
    kind = await kinds.get_kind(tx.conn, row["kind"])
    kinds.validate(kind.result_schema, result.data, "result.data")
    await _check_artifacts(tx.conn, task_id, result.artifacts)
    await _record_result_usage(tx, agent_id, task_id, result)
    await _apply(
        tx,
        row,
        Action.COMPLETE,
        f"agent:{agent_id}",
        set_={"result": result.model_dump_json(exclude_none=True)},
    )
    return await get_task(tx.conn, task_id)


PROGRESS_MIN_INTERVAL_S = 10


async def report_progress(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    message: str,
    percent: int | None = None,
) -> Task:
    """Record a progress note. It is a worker write, so it also renews the lease."""
    row = await _owned(tx, task_id, attempt, agent_id)
    if row["progress_at"] is not None:
        last = datetime.fromisoformat(row["progress_at"])
        wait = PROGRESS_MIN_INTERVAL_S - (tx.now - last).total_seconds()
        if wait > 0:
            raise RateLimitedError(
                f"at most one progress note per {PROGRESS_MIN_INTERVAL_S} s;"
                f" retry in {wait:.1f} s"
            )
    await tx.conn.execute(
        "UPDATE tasks SET progress_message = ?, progress_percent = ?, progress_at = ?,"
        " lease_expires_at = ? WHERE id = ?",
        (
            message,
            percent,
            ts(tx.now),
            _deadline(tx.now, row["lease_ttl_s"]),
            task_id,
        ),
    )
    await record(
        tx,
        f"agent:{agent_id}",
        "task",
        task_id,
        "task.progress",
        {"attempt": attempt, "message": message, "percent": percent},
    )
    return await get_task(tx.conn, task_id)


async def fail(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    error: str,
    reason: str = "worker_fail",
) -> Task:
    """Fail the attempt with the worker's own text.

    The one worker write accepted after the lease deadline (3.3): while the task
    is still claimed by this agent at this attempt, nobody else can hold it, and
    the fail is exactly the expiry the reaper is about to apply, with a better
    `error`. Without this a FAIL sent just after a lapse would be lost."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    await _apply(
        tx,
        row,
        Action.FAIL,
        f"agent:{agent_id}",
        set_={"error": error, "failures": row["failures"] + 1},
        data={"error": error, "reason": reason},
    )
    return await get_task(tx.conn, task_id)


async def block(tx: Tx, agent_id: str, task_id: str, attempt: int, reason: str) -> Task:
    row = await _owned(tx, task_id, attempt, agent_id)
    await _apply(
        tx,
        row,
        Action.BLOCK,
        f"agent:{agent_id}",
        set_={"block_reason": reason},
        data={"reason": reason},
    )
    return await get_task(tx.conn, task_id)


# -- orchestrator / human operations --------------------------------------------


async def unblock(tx: Tx, task_id: str, actor: str) -> Task:
    row = await _row(tx.conn, task_id)
    await _apply(tx, row, Action.UNBLOCK, actor, set_={"block_reason": None})
    return await get_task(tx.conn, task_id)


_RETRY_ERROR_MAX = 500


async def retry(tx: Tx, task_id: str, actor: str) -> Task:
    """Give a `failed` task a fresh window of attempts (`max_attempts` is kept).

    Not a state-machine action: `failed` is terminal for the workers' transitions,
    and only an operator may leave it. `failures` is reset with `attempt` because
    `failures` is what `max_attempts` limits (3.2).
    """
    row = await _row(tx.conn, task_id)
    if row["state"] != S.FAILED:
        raise ConflictError(
            f"task is {row['state']}, not failed", code="task_not_failed"
        )
    await tx.conn.execute(
        "UPDATE tasks SET state = ?, attempt = 0, failures = 0, error = NULL,"
        " assignee_agent_id = NULL, lease_expires_at = NULL, timeout_at = NULL,"
        " progress_message = NULL, progress_percent = NULL, progress_at = NULL,"
        " block_reason = NULL, updated_at = ? WHERE id = ?",
        (S.READY, ts(tx.now), task_id),
    )
    error = row["error"] or ""
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.retried",
        {
            "from": S.FAILED,
            "to": S.READY,
            "task_id": task_id,
            "previous_attempts": row["attempt"],
            "max_attempts": row["max_attempts"],
            "previous_error": error[:_RETRY_ERROR_MAX]
            + ("…" if len(error) > _RETRY_ERROR_MAX else ""),
        },
    )
    return await get_task(tx.conn, task_id)


MAX_DEPENDENCIES = 100
_DEPS_EDITABLE = (S.PENDING, S.AWAITING_APPROVAL, S.READY)


async def set_dependencies(
    tx: Tx, task_id: str, depends_on: list[str], expected_version: str, actor: str
) -> Task:
    """Replace a waiting task's dependencies (task dependencies 3.1)."""
    row = await _row(tx.conn, task_id)
    if row["updated_at"] != expected_version:
        raise ConflictError(
            f"task {task_id} changed since you read it (version"
            f" {row['updated_at']}); read it again and reapply your change",
            code="stale_version",
            details=[row["updated_at"]],
        )
    state = S(row["state"])
    if state not in _DEPS_EDITABLE:
        raise ConflictError(
            f"task {task_id} is {state}; dependencies change only while it waits"
            " (pending, awaiting_approval or ready)",
            code="task_not_waiting",
        )
    deps = sorted(set(depends_on))
    if len(deps) > MAX_DEPENDENCIES:
        raise ValidationFailedError(f"at most {MAX_DEPENDENCIES} dependencies")
    if task_id in deps:
        raise ValidationFailedError(
            "a task cannot depend on itself",
            code="dependency_cycle",
            details=[task_id, task_id],
        )
    dep_states = await _states(tx.conn, deps)
    if missing := set(deps) - dep_states.keys():
        raise NotFoundError(f"dependency not found: {', '.join(sorted(missing))}")
    if cycle := await _path_back(tx.conn, deps, task_id):
        raise ValidationFailedError(
            "these dependencies would close a cycle: " + " -> ".join(cycle),
            code="dependency_cycle",
            details=cycle,
        )

    old = await _deps(tx.conn, task_id)
    await tx.conn.execute("DELETE FROM task_deps WHERE task_id = ?", (task_id,))
    await tx.conn.executemany(
        "INSERT INTO task_deps (task_id, depends_on_id) VALUES (?, ?)",
        [(task_id, d) for d in deps],
    )
    await tx.conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (ts(tx.now), task_id)
    )
    added = sorted(set(deps) - set(old))
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.dependencies_changed",
        {"added": added, "removed": sorted(set(old) - set(deps))},
    )
    for dep in added:
        if dep_states[dep] in (S.FAILED, S.CANCELLED):
            await _dependency_failed_event(tx, task_id, dep, dep_states[dep])
    deps_done = all(s is S.SUCCEEDED for s in dep_states.values())
    if state is S.READY and not deps_done:
        await _apply(tx, row, Action.DEPS_ADDED, actor)
    elif state is S.PENDING and deps_done:
        await _apply(tx, row, Action.DEPS_SATISFIED, actor)
    return await get_task(tx.conn, task_id)


async def _path_back(
    conn: aiosqlite.Connection, starts: list[str], target: str
) -> list[str] | None:
    """A dependency path from one of `starts` back to `target`, if any, as
    [target, start, ..., target]: adding target -> start would close it."""
    parent: dict[str, str | None] = dict.fromkeys(starts)
    frontier = list(starts)
    while frontier:
        marks = ",".join("?" * len(frontier))
        sql = f"SELECT task_id, depends_on_id FROM task_deps WHERE task_id IN ({marks})"  # noqa: S608
        async with conn.execute(sql, frontier) as cur:
            edges = await cur.fetchall()
        frontier = []
        for node, dep in edges:
            if dep in parent:
                continue
            parent[dep] = node
            if dep == target:
                path, at = [], dep
                while at is not None:
                    path.append(at)
                    at = parent[at]
                return [target, *reversed(path)]
            frontier.append(dep)
    return None


async def add_clarification(
    tx: Tx, task_id: str, question: str, answer: str, actor: str
) -> None:
    """Append an answered question to the task spec, so the next claim sees it."""
    row = await _row(tx.conn, task_id)
    spec = row["spec"]
    if "\n## Clarifications\n" not in f"\n{spec}":
        spec += "\n\n## Clarifications\n"
    spec += f"\n- **Asked (attempt {row['attempt']}):** {question}\n"
    spec += f"  **Answered by {actor}:** {answer}\n"
    await tx.conn.execute(
        "UPDATE tasks SET spec = ?, updated_at = ? WHERE id = ?",
        (spec, ts(tx.now), task_id),
    )


async def cancel(tx: Tx, task_id: str, actor: str) -> Task:
    row = await _row(tx.conn, task_id)
    await _apply(tx, row, Action.CANCEL, actor)
    return await get_task(tx.conn, task_id)


async def approve(tx: Tx, task_id: str, actor: str) -> Task:
    row = await _row(tx.conn, task_id)
    dep_states = await _states(tx.conn, await _deps(tx.conn, task_id))
    deps_done = all(s is S.SUCCEEDED for s in dep_states.values())
    await _apply(tx, row, Action.APPROVE, actor, deps_done=deps_done)
    return await get_task(tx.conn, task_id)


async def review(
    tx: Tx,
    task_id: str,
    actor: str,
    reviewer: Scope,
    *,
    accept: bool,
    notes: str = "",
) -> Task:
    row = await _row(tx.conn, task_id)
    if row["review"] == Review.HUMAN and reviewer is not Scope.ADMIN:
        raise ForbiddenError("this task requires human review")
    if accept:
        await _apply(tx, row, Action.ACCEPT, actor, data={"notes": notes})
    else:
        # Rejection is not a failure: `failures` is untouched (3.2).
        spec = row["spec"]
        if notes:
            spec += f"\n\n## Review notes (attempt {row['attempt']})\n\n{notes}"
        await _apply(
            tx, row, Action.REJECT, actor, set_={"spec": spec}, data={"notes": notes}
        )
    return await get_task(tx.conn, task_id)


# -- system ---------------------------------------------------------------------


async def expire_leases(tx: Tx, failed: list[str] | None = None) -> list[str]:
    """Expire every lease past its deadline, whether or not its agent is alive.

    With `failed`, a row that cannot be expired is rolled back alone, logged and
    appended there instead of aborting the others (3.4)."""
    return await _expire_where(
        tx, "lease_expires_at <= ?", ts(tx.now), "lease_expired", failed
    )


async def expire_timeouts(tx: Tx, failed: list[str] | None = None) -> list[str]:
    """Expire every attempt past its wall-clock deadline, however healthy its
    heartbeats: a holder that is alive but never finishes must not hold a task
    forever. It counts as a failure and retries until max_attempts."""
    return await _expire_where(tx, "timeout_at <= ?", ts(tx.now), "timeout", failed)


async def expire_agent_leases(tx: Tx, agent_id: str) -> list[str]:
    """Expire every lease an agent holds, e.g. because it was lost or left.

    All or nothing: the caller ends the agent in the same savepoint."""
    return await _expire_where(tx, "assignee_agent_id = ?", agent_id, "agent_ended")


def _expiry_error(row: aiosqlite.Row, reason: str) -> str:
    holder = row["assignee_agent_id"]
    match reason:
        case "lease_expired":
            return f"lease expired at {row['lease_expires_at']} (holder {holder})"
        case "timeout":
            return f"attempt timed out at {row['timeout_at']} (holder {holder})"
        case _:
            return f"holder {holder} ended; its lease was released"


async def _expire_where(
    tx: Tx,
    condition: str,
    param: str,
    reason: str,
    failed: list[str] | None = None,
) -> list[str]:
    async with tx.conn.execute(
        f"SELECT * FROM tasks WHERE state = 'claimed' AND {condition} ORDER BY id",  # noqa: S608
        (param,),
    ) as cur:
        rows = await cur.fetchall()
    expired: list[str] = []
    for row in rows:
        error = _expiry_error(row, reason)
        try:
            async with tx.savepoint():
                await _apply(
                    tx,
                    row,
                    Action.EXPIRE,
                    "system",
                    set_={
                        "failures": row["failures"] + 1,
                        "error": error,
                        "progress_message": (
                            f"attempt {row['attempt']} ended: "
                            f"{reason.replace('_', ' ')};"
                            f" {'requeued' if row['failures'] + 1 < row['max_attempts'] else 'failed'}"
                        ),
                        "progress_percent": None,
                    },
                    data={
                        "agent_id": row["assignee_agent_id"],
                        "reason": reason,
                        "error": error,
                    },
                )
        except Exception:
            if failed is None:
                raise
            log.exception("could not expire task %s (%s)", row["id"], reason)
            failed.append(row["id"])
            continue
        expired.append(row["id"])
    return expired


# -- internals ------------------------------------------------------------------


async def check_lease(tx: Tx, task_id: str, attempt: int, agent_id: str) -> None:
    """Public ownership check for writes owned by other modules (artifacts)."""
    await _owned(tx, task_id, attempt, agent_id)


async def _owned(
    tx: Tx, task_id: str, attempt: int, agent_id: str, *, allow_expired: bool = False
) -> aiosqlite.Row:
    """Check the caller holds the current, unexpired lease (3.3), inside `tx`.

    `allow_expired` skips only the deadline check (see `fail`)."""
    state = await _agent_state(tx.conn, agent_id)
    if state not in LIVE:
        raise LeaseConflictError(
            f"agent {agent_id} is {state}; stop working", code=f"agent_{state}"
        )
    row = await _row(tx.conn, task_id)
    if row["state"] != S.CLAIMED:
        raise LeaseConflictError(
            f"task {task_id} is {row['state']}, not claimed", code="not_claimed"
        )
    if row["assignee_agent_id"] != agent_id:
        raise LeaseConflictError(
            f"task {task_id} is claimed by another agent", code="not_assignee"
        )
    if row["attempt"] != attempt:
        raise LeaseConflictError(
            f"attempt {attempt} of task {task_id} was superseded by attempt"
            f" {row['attempt']}; stop working on it",
            code="stale_attempt",
        )
    if not allow_expired and row["lease_expires_at"] <= ts(tx.now):
        raise LeaseConflictError(
            f"lease on task {task_id} expired at {row['lease_expires_at']};"
            " stop working on it",
            code="lease_expired",
        )
    return row


async def _apply(
    tx: Tx,
    row: aiosqlite.Row,
    action: Action,
    actor: str,
    *,
    set_: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    deps_done: bool = True,
) -> TaskState:
    """Validate and persist one transition, its event, and its knock-on effects."""
    old = S(row["state"])
    ctx = TransitionContext(
        failures=row["failures"],
        max_attempts=row["max_attempts"],
        review=Review(row["review"]),
        deps_done=deps_done,
    )
    new = transition(old, action, ctx)
    fields: dict[str, Any] = {"state": new, "updated_at": ts(tx.now), **(set_ or {})}
    if old is S.CLAIMED and new is not S.CLAIMED:
        fields |= {
            "assignee_agent_id": None,
            "lease_expires_at": None,
            "timeout_at": None,
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
        f"task.{action}",
        {"from": old, "to": new, "attempt": row["attempt"], **(data or {})},
    )
    # Decisions that waited on the old state are moot once the task leaves it
    # (an answered decision is already closed before its transition runs).
    if new in TERMINAL:
        await decisions.cancel_open(tx, "task", row["id"])
    elif old in decisions.WAITS_IN and new is not old:
        await decisions.cancel_open(tx, "task", row["id"], decisions.WAITS_IN[old])
    if new is S.IN_REVIEW:
        review = Review(row["review"])
        await decisions.open_decision(
            tx,
            subject_type="task",
            subject_id=row["id"],
            kind=DecisionKind.REVIEW,
            question=f"Review the result of {row['title']!r} (attempt {row['attempt']}).",
            options=list(_REVIEW_OPTIONS),
            audience=Audience.HUMAN
            if review is Review.HUMAN
            else Audience.ORCHESTRATOR,
            requested_by=actor,
        )
    if new is S.SUCCEEDED:
        await _promote_dependents(tx, row["id"])
    elif new in (S.FAILED, S.CANCELLED):
        await _flag_dependents(tx, row["id"], new)
    return new


_APPROVAL_OPTIONS = ("approve", "reject")
_REVIEW_OPTIONS = ("accept", "reject")


async def _created_last_hour(tx: Tx, actor: str) -> int:
    async with tx.conn.execute(
        "SELECT count(*) FROM tasks WHERE created_by = ? AND created_at > ?",
        (actor, ts(tx.now - timedelta(hours=1))),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


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


def _apply_kind_defaults(body: TaskCreate, kind: TaskKind) -> TaskCreate:
    """Fill fields the creator left unset from the kind; validate the input."""
    kinds.validate(kind.input_schema, body.input, "input")
    defaults = {
        "required_capabilities": kind.default_capabilities,
        "lease_ttl_s": kind.default_lease_ttl_s,
        "max_attempts": kind.default_max_attempts,
        "timeout_s": kind.default_timeout_s,
        "review": kind.default_review,
    }
    update = {
        field: value
        for field, value in defaults.items()
        if value is not None and field not in body.model_fields_set
    }
    if kind.default_knowledge:  # pins merge rather than replace
        update["knowledge"] = list(
            dict.fromkeys([*kind.default_knowledge, *body.knowledge])
        )
    return body.model_copy(update=update)


async def _check_project_and_pins(conn: aiosqlite.Connection, body: TaskCreate) -> None:
    if body.project is not None:
        async with conn.execute(
            "SELECT 1 FROM projects WHERE slug = ?", (body.project,)
        ) as cur:
            if await cur.fetchone() is None:
                raise NotFoundError(
                    f"unknown project: {body.project}", code="unknown_project"
                )
    if body.knowledge:
        found = set((await _states_of(conn, "knowledge_pages", body.knowledge)).keys())
        if missing := sorted(set(body.knowledge) - found):
            raise NotFoundError(f"knowledge page not found: {', '.join(missing)}")


async def _states_of(
    conn: aiosqlite.Connection, table: str, ids: list[str]
) -> dict[str, str]:
    marks = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT id, state FROM {table} WHERE id IN ({marks})",  # noqa: S608
        ids,
    ) as cur:
        return {r["id"]: r["state"] for r in await cur.fetchall()}


async def _record_result_usage(
    tx: Tx, agent_id: str, task_id: str, result: TaskResult
) -> None:
    """Usage reported in the result counts against its account (recorded only)."""
    usage = result.usage
    if usage is None or usage.account_id is None:
        return
    async with tx.conn.execute(
        "SELECT 1 FROM accounts WHERE id = ?", (usage.account_id,)
    ) as cur:
        if await cur.fetchone() is None:
            raise ValidationFailedError(
                "result.usage.account_id is not a known account",
                details=[usage.account_id],
            )
    await insert_usage(
        tx,
        usage.account_id,
        agent_id,
        task_id,
        **usage.model_dump(exclude={"account_id"}),
    )


async def _check_artifacts(
    conn: aiosqlite.Connection, task_id: str, artifact_ids: list[str]
) -> None:
    if not artifact_ids:
        return
    marks = ",".join("?" * len(artifact_ids))
    async with conn.execute(
        f"SELECT id FROM artifacts WHERE task_id = ? AND id IN ({marks})",  # noqa: S608
        (task_id, *artifact_ids),
    ) as cur:
        found = {r["id"] for r in await cur.fetchall()}
    if missing := [a for a in artifact_ids if a not in found]:
        raise ValidationFailedError(
            "result lists artifacts that were not uploaded for this task",
            details=missing,
        )


async def _agent_state(conn: aiosqlite.Connection, agent_id: str) -> AgentState:
    async with conn.execute(
        "SELECT state FROM agents WHERE id = ?", (agent_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"agent not found: {agent_id}")
    return AgentState(row["state"])


async def _row(conn: aiosqlite.Connection, task_id: str) -> aiosqlite.Row:
    async with conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"task not found: {task_id}")
    return row


async def _states(conn: aiosqlite.Connection, ids: list[str]) -> dict[str, TaskState]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT id, state FROM tasks WHERE id IN ({marks})",  # noqa: S608
        ids,
    ) as cur:
        return {r["id"]: S(r["state"]) for r in await cur.fetchall()}


async def _deps(conn: aiosqlite.Connection, task_id: str) -> list[str]:
    async with conn.execute(
        "SELECT depends_on_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_id",
        (task_id,),
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def _to_task(conn: aiosqlite.Connection, row: aiosqlite.Row) -> Task:
    data = dict(row)
    for f in _JSON_FIELDS:
        data[f] = json.loads(data[f])
    if data["result"] is not None:
        data["result"] = json.loads(data["result"])
    data["requires_approval"] = bool(data["requires_approval"])
    data["depends_on"] = await _deps(conn, row["id"])
    async with conn.execute(
        "SELECT page_id FROM task_knowledge WHERE task_id = ? ORDER BY page_id",
        (row["id"],),
    ) as cur:
        data["knowledge"] = [r[0] for r in await cur.fetchall()]
    return Task.model_validate(data)


def _deadline(now: datetime, ttl_s: int) -> str:
    return ts(now + timedelta(seconds=ttl_s))
