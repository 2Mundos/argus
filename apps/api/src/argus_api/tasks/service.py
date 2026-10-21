"""Task operations. Every write takes an open `Tx` and records its events in it.

See docs/specs/worker-lifecycle.spec.md 3.2 and 3.3.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS, LOCAL_PROVIDER
from argus_core.agents import LIVE, AgentState
from argus_core.auth import Scope
from argus_core.complexity import Complexity, ComplexitySource, classify
from argus_core.custom_accounts import CustomConfig
from argus_core.decisions import Audience, DecisionKind
from argus_core.kinds import TaskKind
from argus_core.quota import failure_class
from argus_core.task_edit import CLEARABLE, TaskAttempt, TaskChanges, editable_fields
from argus_core.tasks import (
    TERMINAL,
    Action,
    Failover,
    PromptTrust,
    Review,
    ServingAccount,
    Task,
    TaskCreate,
    TaskResult,
    TaskState,
    TransitionContext,
    transition,
    validate_milestone,
)
from pydantic import ValidationError

from argus_api.access import record_lease_grant
from argus_api.accounts.eligibility import engine_pool
from argus_api.accounts.usage import insert_usage
from argus_api.db import Snapshot, Tx, ts
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
from argus_api.modeltrace import service as modeltrace

log = logging.getLogger(__name__)
S = TaskState
_JSON_FIELDS = ("acceptance", "input", "required_capabilities")

# `blocked` for want of a usable model account (account selection 3.6). Rows written
# before the rename carry `no_account`; both are read and released alike.
AWAITING_QUOTA = "awaiting_quota"
QUOTA_WAIT_REASONS = (AWAITING_QUOTA, "no_account")


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
    # Worker-authored child tasks: immutable worker trust; always await approval so
    # injected prompts cannot silently become ready work for another agent.
    if as_worker is not None:
        body = body.model_copy(update={"requires_approval": True})
        prompt_trust = PromptTrust.WORKER
    else:
        prompt_trust = PromptTrust.ORCHESTRATOR
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
    complexity = body.complexity or classify(body.title, body.spec)
    source = (
        ComplexitySource.EXPLICIT if body.complexity else ComplexitySource.HEURISTIC
    )
    await tx.conn.execute(
        "INSERT INTO tasks (id, parent_id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " prompt_trust, lease_ttl_s, max_attempts, project, timeout_s, created_by,"
        " created_at, updated_at, complexity, complexity_source, account_providers)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            prompt_trust,
            body.lease_ttl_s,
            body.max_attempts,
            body.project,
            body.timeout_s,
            actor,
            now,
            now,
            complexity,
            source,
            _providers_json(body.account_providers),
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
    tx: Tx,
    agent_id: str,
    capabilities: list[str],
    *,
    default_timeout_s: int = 0,
    runtime_providers: list[str] | None = None,
) -> Task | None:
    """Claim the highest-priority, oldest ready task the agent is capable of.

    With `runtime_providers` and a fleet that manages model accounts, a task is
    only claimed when the caller can run it now: it already holds an engine lease of
    a provider the task allows, or such an account has a free lease slot (a task
    restricted to `account_providers` allows only those; local harness spec 3). A
    task nobody can serve (no free slot and no idle holder) is parked as
    `awaiting_quota` instead, without spending an attempt, and is woken when an
    account frees up (account selection 3.6); one an idle holder elsewhere can serve
    stays ready for it. Without `runtime_providers` (or with an empty list) only
    restricted tasks are gated.

    The attempt's wall-clock deadline is the task's `timeout_s` (which creation
    filled from its kind), else `default_timeout_s`; 0 means no deadline.
    """
    state = await _agent_state(tx.conn, agent_id)
    if state is not AgentState.ACTIVE:
        raise ConflictError(
            f"agent {agent_id} is {state}; it may not claim new tasks",
            code=f"agent_{state}",
        )
    await _check_engine_lease(tx, agent_id)
    if runtime_providers and await _record_runtime_providers(
        tx, agent_id, runtime_providers
    ):
        # A runtime that can apply a provider no live agent could before makes
        # waiting tasks servable: wake them (bounded) before choosing.
        await wake_waiting(tx)
    if await awaiting_quota(tx.conn, tx.now):
        return None
    async with tx.conn.execute(
        "SELECT * FROM tasks t WHERE state = 'ready' AND NOT EXISTS ("
        "  SELECT 1 FROM json_each(t.required_capabilities) rc"
        "  WHERE rc.value NOT IN (SELECT value FROM json_each(?)))"
        " ORDER BY priority DESC, created_at, id",
        (json.dumps(capabilities),),
    ) as cur:
        candidates = await cur.fetchall()
    gated = bool(runtime_providers) and await _manages_accounts(tx.conn)
    row = None
    for candidate in candidates:
        restricted = candidate["account_providers"] is not None
        if not restricted and not gated:
            row = candidate
            break
        providers = (
            json.loads(candidate["account_providers"])
            if restricted
            else sorted(ENGINE_PROVIDERS)
        )
        if await _restriction_met(tx, providers, runtime_providers, agent_id):
            row = candidate
            break
        if gated and not await can_serve(tx, candidate["account_providers"]):
            await _apply(
                tx,
                candidate,
                Action.BLOCK,
                f"agent:{agent_id}",
                set_={"block_reason": AWAITING_QUOTA},
                data={"reason": AWAITING_QUOTA},
            )
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
            "progress_kind": None,  # a milestone belongs to one attempt
            "progress_data": None,
            "output_at": None,
            "lease_expires_at": _deadline(tx.now, row["lease_ttl_s"]),
            "timeout_at": (
                _deadline(tx.now, timeout)
                if (timeout := row["timeout_s"] or default_timeout_s) > 0
                else None
            ),
        },
        data={"attempt": attempt, "agent_id": agent_id},
    )
    await record_lease_grant(tx, row["id"], agent_id, attempt)
    task = await get_task(tx.conn, row["id"])
    await _snapshot_attempt(tx, task, agent_id)
    return task


async def prefers_local(conn: aiosqlite.Connection, task_id: str) -> bool:
    """A trivial task ranks the local model first (task complexity spec 3)."""
    row = await _row(conn, task_id)
    return row["complexity"] == Complexity.TRIVIAL


async def account_providers(
    conn: aiosqlite.Connection, task_id: str
) -> list[str] | None:
    """The task's provider restriction, None when unrestricted."""
    row = await _row(conn, task_id)
    raw = row["account_providers"]
    return None if raw is None else json.loads(raw)


async def _restriction_met(
    tx: Tx,
    providers: list[str],
    runtime_providers: list[str] | None,
    agent_id: str | None = None,
) -> bool:
    """Can the caller start a task allowing `providers` now: the runtime applies one
    of them and either the caller already holds an engine lease of one, or an account
    of one is usable (available or past its reset, below `max_leases`; an
    unreachable local model is disabled by its probe, so it never counts)."""
    wanted = sorted(set(providers) & set(runtime_providers or []))
    if not wanted:
        return False
    async with tx.conn.execute(
        "SELECT EXISTS (SELECT 1 FROM account_leases l JOIN accounts a"
        "  ON a.id = l.account_id WHERE l.agent_id = ? AND l.role = 'engine'"
        "  AND l.released_at IS NULL"
        "  AND a.provider IN (SELECT value FROM json_each(?)))"
        " OR EXISTS (SELECT 1 FROM accounts a"
        " WHERE a.provider IN (SELECT value FROM json_each(?))"
        "  AND (a.state = 'available'"
        "   OR (a.state = 'exhausted' AND a.exhausted_until <= ?))"
        "  AND (a.max_leases IS NULL OR a.max_leases >"
        "   (SELECT count(*) FROM account_leases l"
        "    WHERE l.account_id = a.id AND l.released_at IS NULL)))",
        (agent_id, json.dumps(wanted), json.dumps(wanted), ts(tx.now)),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0])


async def _manages_accounts(conn: aiosqlite.Connection) -> bool:
    """Does the fleet manage model accounts at all (else claims are never gated)?"""
    async with conn.execute(
        "SELECT EXISTS (SELECT 1 FROM accounts"
        " WHERE provider IN (SELECT value FROM json_each(?)))",
        (json.dumps(sorted(ENGINE_PROVIDERS)),),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0])


async def _record_runtime_providers(
    tx: Tx, agent_id: str, runtime_providers: list[str]
) -> bool:
    """Store the providers the agent's runtime applies (its latest claim). True
    when that adds a provider no live agent declared before."""
    wanted = sorted(set(runtime_providers))
    before = await _served_providers(tx.conn)
    async with tx.conn.execute(
        "SELECT runtime_providers FROM agents WHERE id = ?", (agent_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None or (row[0] and json.loads(row[0]) == wanted):
        return False
    await tx.conn.execute(
        "UPDATE agents SET runtime_providers = ? WHERE id = ?",
        (json.dumps(wanted), agent_id),
    )
    return before is not None and not set(wanted) <= before


async def _served_providers(conn: aiosqlite.Connection) -> set[str] | None:
    """Providers some live (active) agent's runtime applies, as declared on its
    claims; None when no live agent declared any (older workers): then every
    provider counts, as before."""
    async with conn.execute(
        "SELECT DISTINCT p.value FROM agents a, json_each(a.runtime_providers) p"
        " WHERE a.state = 'active' AND a.runtime_providers IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    return {r[0] for r in rows} or None


async def engine_capacity(tx: Tx) -> list[list[Any]]:
    """`[provider, slots]` of each usable engine account, cloud before the local
    model: how many more tasks it can serve now. Slots are the free lease slots plus
    the engine leases held by agents with no claimed task (they can take one at
    once); None means no lease limit. Accounts with no slot are left out, and so are
    accounts of a provider no live agent's runtime can apply (a `custom` account no
    worker runs is no capacity)."""
    served = await _served_providers(tx.conn)
    async with tx.conn.execute(
        "SELECT a.provider, a.max_leases,"
        " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL) AS active,"
        " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL AND l.role = 'engine' AND NOT EXISTS ("
        "   SELECT 1 FROM tasks t WHERE t.state = 'claimed'"
        "   AND t.assignee_agent_id = l.agent_id)) AS idle"
        " FROM accounts a WHERE a.state = 'available'"
        "  OR (a.state = 'exhausted' AND a.exhausted_until <= ?)"
        " ORDER BY a.priority DESC, a.id",
        (ts(tx.now),),
    ) as cur:
        rows = await cur.fetchall()
    out: list[list[Any]] = []
    for r in rows:
        if r["provider"] not in ENGINE_PROVIDERS:
            continue
        if served is not None and r["provider"] not in served:
            continue
        if r["max_leases"] is None:
            out.append([r["provider"], None])
            continue
        slots = max(0, r["max_leases"] - r["active"]) + r["idle"]
        if slots > 0:
            out.append([r["provider"], slots])
    out.sort(key=lambda c: c[0] == LOCAL_PROVIDER)  # stable: cloud first
    return out


def _allows(account_providers: str | None, provider: str) -> bool:
    return account_providers is None or provider in json.loads(account_providers)


async def can_serve(tx: Tx, account_providers: str | None) -> bool:
    """Could any agent run a task with this (stored) restriction now?"""
    return any(
        _allows(account_providers, provider)
        for provider, _ in await engine_capacity(tx)
    )


async def wake_waiting(tx: Tx, actor: str = "system") -> list[str]:
    """Return tasks waiting for an account (`awaiting_quota`, or the older
    `no_account`) to ready, highest priority first, but only as many as the usable
    engine accounts can serve now: each woken task takes one slot of an account its
    `account_providers` allow. No thundering herd: the rest keep waiting for the
    next release, refill or account change. Returns the woken task ids."""
    capacity = await engine_capacity(tx)
    if not capacity:
        return []
    async with tx.conn.execute(
        "SELECT id, account_providers FROM tasks WHERE state = 'blocked'"
        " AND block_reason IN (?, ?) ORDER BY priority DESC, created_at, id",
        QUOTA_WAIT_REASONS,
    ) as cur:
        rows = await cur.fetchall()
    woken: list[str] = []
    for r in rows:
        slot = next(
            (
                c
                for c in capacity
                if (c[1] is None or c[1] > 0) and _allows(r["account_providers"], c[0])
            ),
            None,
        )
        if slot is None:
            continue
        if slot[1] is not None:
            slot[1] -= 1
        await unblock(tx, r["id"], actor)
        woken.append(r["id"])
    return woken


async def awaiting_quota(conn: aiosqlite.Connection, now: datetime | None) -> bool:
    """True when some engine account is held back and none is usable: a claim would
    only burn an attempt on a provider limit (one rule: `engine_pool`)."""
    return (await engine_pool(conn, now)).awaiting_quota


async def _check_engine_lease(tx: Tx, agent_id: str) -> None:
    """A claim whose engine lease sits on an account that is disabled, or exhausted
    with its reset ahead, cannot run: `409 lease_account_unusable`. The channel
    drops that account, leases again and recycles the runtime."""
    async with tx.conn.execute(
        "SELECT a.name, a.provider, a.state, a.exhausted_until FROM account_leases l"
        " JOIN accounts a ON a.id = l.account_id WHERE l.agent_id = ?"
        " AND l.role = 'engine' AND l.released_at IS NULL",
        (agent_id,),
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        if r["provider"] == LOCAL_PROVIDER:
            continue
        held_back = r["state"] == "exhausted" and (
            not r["exhausted_until"]
            or datetime.fromisoformat(r["exhausted_until"]) > tx.now
        )
        if r["state"] == "disabled" or held_back:
            raise ConflictError(
                f"engine account {r['name']} is {r['state']}; lease another",
                code="lease_account_unusable",
            )


RUNTIME_SWITCH_FAILED = "runtime_switch_failed"


async def block_switch_failed(
    tx: Tx, agent_id: str, task_id: str, attempt: int, error: str
) -> Task:
    """The old runtime did not stop, so the new account cannot be applied safely:
    block the task (`runtime_switch_failed`) with `attempt` and `failures` as they
    were. Nothing releases it automatically (`wake_waiting` only wakes quota waits):
    the task stays blocked until an operator acts."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    await _apply(
        tx,
        row,
        Action.BLOCK,
        f"agent:{agent_id}",
        set_={
            "error": error,
            "block_reason": RUNTIME_SWITCH_FAILED,
            "progress_message": "account switch failed; the old runtime did not stop",
            "progress_percent": None,
            "progress_at": ts(tx.now),
        },
        data={"reason": RUNTIME_SWITCH_FAILED, "error": error},
    )
    return await get_task(tx.conn, task_id)


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
    kind: str | None = None,
    data: dict[str, Any] | None = None,
) -> Task:
    """Record a progress note. It is a worker write, so it also renews the lease.

    A note with a `kind` is a milestone (progress-milestones spec): it replaces the
    stored one and is not rate limited unless the kind repeats. A note without a kind
    leaves the stored milestone alone.
    """
    try:
        validate_milestone(kind, data)
    except ValueError as err:
        raise ValidationFailedError(str(err)) from None
    row = await _owned(tx, task_id, attempt, agent_id)
    if row["progress_at"] is not None and (
        kind is None or kind == row["progress_kind"]
    ):
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
    if kind is not None:
        await tx.conn.execute(
            "UPDATE tasks SET progress_kind = ?, progress_data = ? WHERE id = ?",
            (kind, json.dumps({**(data or {}), "at": ts(tx.now)}), task_id),
        )
    await record(
        tx,
        f"agent:{agent_id}",
        "task",
        task_id,
        "task.progress",
        {
            "attempt": attempt,
            "message": message,
            "percent": percent,
            **({"kind": kind, "data": data or {}} if kind is not None else {}),
        },
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
    `error`. Without this a FAIL sent just after a lapse would be lost.

    Reason `quota` (the provider's limit, not the task's fault) is not a failure:
    `failures` stays. The task returns to ready when some agent can run it now (a
    free account slot, or an idle agent holding a fitting lease); otherwise it waits
    as `awaiting_quota` until `wake_waiting` frees it, instead of bouncing between
    agents that cannot lease an account (account selection 3.6)."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    quota = reason == "quota"
    if (
        quota
        and await _manages_accounts(tx.conn)
        and not await can_serve(tx, row["account_providers"])
    ):
        await _apply(
            tx,
            row,
            Action.BLOCK,
            f"agent:{agent_id}",
            set_={
                "error": error,
                "block_reason": AWAITING_QUOTA,
                "progress_message": f"provider quota; waiting for an account: {error}",
            },
            data={"error": error, "reason": reason},
        )
        return await get_task(tx.conn, task_id)
    await _apply(
        tx,
        row,
        Action.FAIL,
        f"agent:{agent_id}",
        set_={"error": error}
        | (
            {"progress_message": f"provider quota; requeued: {error}"}
            if quota
            else {"failures": row["failures"] + 1}
        ),
        data={"error": error, "reason": reason},
        counts_failure=not quota,
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


async def pause_for_quota(
    tx: Tx, agent_id: str, task_id: str, attempt: int, note: str, *, wait: bool
) -> Task:
    """A provider quota ended the attempt (not the task's fault): back to `ready`
    (or, with `wait`, `blocked` as `awaiting_quota` because no account is left)
    with `failures` and `attempt` untouched, so `max_attempts` is not consumed.
    Fenced like `fail`, which it replaces for this case."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    outcome = "waiting for quota" if wait else "requeued"
    await _apply(
        tx,
        row,
        Action.BLOCK if wait else Action.REQUEUE,
        f"agent:{agent_id}",
        set_={
            "progress_message": f"attempt {attempt} paused: provider quota reached;"
            f" {outcome}",
            "progress_percent": None,
            "progress_at": ts(tx.now),
            **({"block_reason": AWAITING_QUOTA} if wait else {}),
        },
        data={"reason": "quota_exhausted", "error": note[:500]},
    )
    return await get_task(tx.conn, task_id)


async def await_quota_ready(
    tx: Tx, agent_id: str, capabilities: list[str]
) -> list[str]:
    """Block the `ready` tasks this agent could claim as `awaiting_quota`: it has no
    usable account, so claiming would only fail. `attempt` and `failures` stay;
    `release_waiting_tasks` returns them to ready. Returns the task ids."""
    async with tx.conn.execute(
        "SELECT * FROM tasks t WHERE state = 'ready' AND NOT EXISTS ("
        "  SELECT 1 FROM json_each(t.required_capabilities) rc"
        "  WHERE rc.value NOT IN (SELECT value FROM json_each(?))) ORDER BY id",
        (json.dumps(capabilities),),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await _apply(
            tx,
            row,
            Action.BLOCK,
            f"agent:{agent_id}",
            set_={"block_reason": AWAITING_QUOTA},
            data={"reason": AWAITING_QUOTA},
        )
    return [r["id"] for r in rows]


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
        " progress_kind = NULL, progress_data = NULL, output_at = NULL,"
        " block_reason = NULL, archived_at = NULL, updated_at = ? WHERE id = ?",
        (S.READY, ts(tx.now), task_id),
    )
    await tx.conn.execute("DELETE FROM task_log WHERE task_id = ?", (task_id,))
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


_ARCHIVABLE = sorted(TERMINAL)


async def archive(tx: Tx, task_id: str, actor: str) -> Task:
    """Archive a terminal task. Archiving an archived task changes nothing."""
    row = await _row(tx.conn, task_id)
    if row["archived_at"] is None:
        _require_archivable(row)
        await _set_archived(tx, row, ts(tx.now), actor)
    return await get_task(tx.conn, task_id)


async def unarchive(tx: Tx, task_id: str, actor: str) -> Task:
    """Bring an archived task back into listings and totals. Idempotent."""
    row = await _row(tx.conn, task_id)
    if row["archived_at"] is not None:
        await _set_archived(tx, row, None, actor)
    return await get_task(tx.conn, task_id)


async def archive_matching(
    tx: Tx,
    actor: str,
    *,
    states: list[TaskState],
    project: str | None = None,
    older_than_days: int | None = None,
    dry_run: bool = False,
) -> int:
    """Archive every unarchived task in `states` (terminal only), optionally in
    one project and last changed more than `older_than_days` ago. With `dry_run`
    nothing is written; either way the count is of tasks that match now."""
    if bad := sorted(set(states) - TERMINAL):
        raise ValidationFailedError(
            f"only {', '.join(_ARCHIVABLE)} tasks can be archived, not {', '.join(bad)}",
            code="state_not_archivable",
        )
    where = ["archived_at IS NULL", f"state IN ({','.join('?' * len(states))})"]
    params: list[Any] = [*states]
    if project is not None:
        where.append("project = ?")
        params.append(project)
    if older_than_days is not None:
        where.append("updated_at < ?")
        params.append(ts(tx.now - timedelta(days=older_than_days)))
    async with tx.conn.execute(
        f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY id",  # noqa: S608
        params,
    ) as cur:
        rows = list(await cur.fetchall())
    if not dry_run:
        for row in rows:
            await _set_archived(tx, row, ts(tx.now), actor)
    return len(rows)


def _require_archivable(row: aiosqlite.Row) -> None:
    if S(row["state"]) not in TERMINAL:
        raise ConflictError(
            f"task is {row['state']}; only {', '.join(_ARCHIVABLE)} tasks can be"
            " archived",
            code="task_not_terminal",
        )


async def _set_archived(
    tx: Tx, row: aiosqlite.Row, archived_at: str | None, actor: str
) -> None:
    await tx.conn.execute(
        "UPDATE tasks SET archived_at = ?, updated_at = ? WHERE id = ?",
        (archived_at, ts(tx.now), row["id"]),
    )
    kind = "task.unarchived" if archived_at is None else "task.archived"
    await record(
        tx,
        actor,
        "task",
        row["id"],
        kind,
        {"task_id": row["id"], "state": row["state"]},
    )


MAX_DEPENDENCIES = 100
_DEPS_EDITABLE = (S.PENDING, S.AWAITING_APPROVAL, S.READY)


async def set_dependencies(
    tx: Tx, task_id: str, depends_on: list[str], expected_version: str, actor: str
) -> Task:
    """Replace a waiting task's dependencies (task dependencies 3.1): an edit of
    `depends_on` alone, which keeps its own code for a task that is not waiting."""
    return await edit_task(
        tx,
        task_id,
        TaskChanges(depends_on=depends_on),
        expected_version,
        actor,
        state_code="task_not_waiting",
    )


async def edit_task(
    tx: Tx,
    task_id: str,
    changes: TaskChanges,
    expected_version: str,
    actor: str,
    *,
    state_code: str = "task_not_editable",
) -> Task:
    """Edit a task's definition (task edit spec). The one path for REST, MCP and
    the dashboard: every check runs before the first write, so a refused edit
    leaves the task as it was."""
    if unknown := changes.unknown():
        raise ValidationFailedError(
            "these fields cannot be edited: " + ", ".join(unknown),
            code="immutable_field",
            details=unknown,
        )
    given = changes.given()
    if not given:
        raise ValidationFailedError(
            "nothing to change: name at least one field",
            details=["changes: nothing to change"],
        )
    row = await _row(tx.conn, task_id)
    if row["updated_at"] != expected_version:
        raise ConflictError(
            f"task {task_id} changed since you read it (version"
            f" {row['updated_at']}); read it again and reapply your change",
            code="stale_version",
            details=[row["updated_at"]],
        )
    state = S(row["state"])
    allowed = editable_fields(state, archived=row["archived_at"] is not None)
    if rejected := [f for f in given if f not in allowed]:
        raise ConflictError(
            f"task {task_id} is {state}{' (archived)' if row['archived_at'] else ''};"
            f" {', '.join(rejected)} cannot be edited now",
            code=state_code,
            details=[
                f"state: {state}",
                f"not editable: {', '.join(rejected)}",
                f"editable: {', '.join(allowed) or 'none'}",
            ],
        )
    current = await get_task(tx.conn, task_id)
    new = _normalized_edit(row, given)
    changed = {f: v for f, v in new.items() if v != getattr(current, f)}
    if not changed:
        return current  # nothing differs: no write, same version

    deps = changed.get("depends_on")
    dep_states: dict[str, TaskState] = {}
    if deps is not None:
        dep_states = await _check_dependencies(tx.conn, task_id, deps)
    if (pins := changed.get("knowledge")) is not None:
        await _check_new_pins(
            tx.conn, row["project"], [p for p in pins if p not in current.knowledge]
        )

    sets: dict[str, Any] = {
        f: json.dumps(v) if f in _JSON_FIELDS else v
        for f, v in changed.items()
        if f not in ("knowledge", "depends_on", "account_providers")
    }
    if "complexity" in sets:
        sets["complexity_source"] = ComplexitySource.EXPLICIT
    if "account_providers" in changed:
        sets["account_providers"] = _providers_json(changed["account_providers"])
    if sets:
        assignments = ", ".join(f"{k} = ?" for k in sets)
        await tx.conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",  # noqa: S608 - keys are code-controlled
            (*sets.values(), task_id),
        )
    if pins is not None:
        await tx.conn.execute(
            "DELETE FROM task_knowledge WHERE task_id = ?", (task_id,)
        )
        await tx.conn.executemany(
            "INSERT INTO task_knowledge (task_id, page_id) VALUES (?, ?)",
            [(task_id, p) for p in pins],
        )
    if deps is not None:
        await tx.conn.execute("DELETE FROM task_deps WHERE task_id = ?", (task_id,))
        await tx.conn.executemany(
            "INSERT INTO task_deps (task_id, depends_on_id) VALUES (?, ?)",
            [(task_id, d) for d in deps],
        )
        added = sorted(set(deps) - set(current.depends_on))
        await record(
            tx,
            actor,
            "task",
            task_id,
            "task.dependencies_changed",
            {"added": added, "removed": sorted(set(current.depends_on) - set(deps))},
        )
        for dep in added:
            if dep_states[dep] in (S.FAILED, S.CANCELLED):
                await _dependency_failed_event(tx, task_id, dep, dep_states[dep])
        deps_done = all(s is S.SUCCEEDED for s in dep_states.values())
        if state is S.READY and not deps_done:
            await _apply(tx, row, Action.DEPS_ADDED, actor)
        elif state is S.PENDING and deps_done:
            await _apply(tx, row, Action.DEPS_SATISFIED, actor)
    # A version that did not move would let a stale editor through, so it only grows.
    version = ts(max(tx.now, datetime.fromisoformat(row["updated_at"]) + _TICK))
    await tx.conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (version, task_id)
    )
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.edited",
        {
            "from_version": row["updated_at"],
            "to_version": version,
            "changes": {
                f: _diff(f, getattr(current, f), v) for f, v in changed.items()
            },
        },
    )
    return await get_task(tx.conn, task_id)


_TICK = timedelta(microseconds=1)


def _normalized_edit(row: aiosqlite.Row, given: dict[str, Any]) -> dict[str, Any]:
    """The values an edit would store, checked with the creation rules: a clear
    becomes the empty value, lists are de-duplicated as at creation."""
    errors: list[str] = []
    values: dict[str, Any] = {}
    for field, value in given.items():
        if value is None:
            if field == "account_providers":
                values[field] = None
            elif field not in CLEARABLE:
                errors.append(f"{field}: cannot be cleared")
                continue
            else:
                values[field] = "" if field == "spec" else []
        else:
            values[field] = value
    if "required_capabilities" in values:
        values["required_capabilities"] = sorted(set(values["required_capabilities"]))
    if "knowledge" in values:
        values["knowledge"] = list(dict.fromkeys(values["knowledge"]))
    if "depends_on" in values:
        values["depends_on"] = sorted(set(values["depends_on"]))
    if "account_providers" in values:
        # an empty list clears to "unrestricted", like the other list fields
        values["account_providers"] = (
            sorted(set(values["account_providers"]))
            if values["account_providers"]
            else None
        )
    try:
        TaskCreate.model_validate(
            {"project": row["project"], "kind": row["kind"], "title": row["title"]}
            | values
        )
    except ValidationError as exc:
        errors += [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
            if e["loc"] and e["loc"][0] in values
        ]
    if errors:
        raise ValidationFailedError(
            "invalid values: " + "; ".join(errors), details=errors
        )
    return values


def _providers_json(providers: list[str] | None) -> str | None:
    return None if providers is None else json.dumps(providers)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _diff(field: str, old: Any, new: Any) -> dict[str, Any]:
    """What the audit keeps of one change. Spec and acceptance text never enter the
    event log (it is streamed and shown): only sizes and digests do."""
    if field in ("title", "priority", "complexity", "account_providers"):
        return {"from": old, "to": new}
    if field in ("required_capabilities", "knowledge", "depends_on"):
        return {
            "added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
        }
    texts = (old, new) if field == "spec" else (json.dumps(old), json.dumps(new))
    sizes = (len(old), len(new))
    return {
        "from_size": sizes[0],
        "to_size": sizes[1],
        "from_sha256": _digest(texts[0]),
        "to_sha256": _digest(texts[1]),
    }


async def _check_dependencies(
    conn: aiosqlite.Connection, task_id: str, deps: list[str]
) -> dict[str, TaskState]:
    """The rules for a task's dependencies (task dependencies 3.1); returns the
    state of each. Raises before anything is written."""
    if len(deps) > MAX_DEPENDENCIES:
        raise ValidationFailedError(f"at most {MAX_DEPENDENCIES} dependencies")
    if task_id in deps:
        raise ValidationFailedError(
            "a task cannot depend on itself",
            code="dependency_cycle",
            details=[task_id, task_id],
        )
    dep_states = await _states(conn, deps)
    if missing := sorted(set(deps) - dep_states.keys()):
        raise NotFoundError(
            f"dependency not found: {', '.join(missing)}",
            details=[f"depends_on: {m}" for m in missing],
        )
    if cycle := await _path_back(conn, deps, task_id):
        raise ValidationFailedError(
            "these dependencies would close a cycle: " + " -> ".join(cycle),
            code="dependency_cycle",
            details=cycle,
        )
    return dep_states


async def _check_new_pins(
    conn: aiosqlite.Connection, project: str, added: list[str]
) -> None:
    """Pages an edit adds must exist and belong to the task's project or `global`."""
    if not added:
        return
    await _check_pages_exist(conn, added)
    marks = ",".join("?" * len(added))
    async with conn.execute(
        f"SELECT id FROM knowledge_pages WHERE id IN ({marks})"  # noqa: S608
        " AND project NOT IN (?, 'global')",
        [*added, project],
    ) as cur:
        foreign = sorted(r[0] for r in await cur.fetchall())
    if foreign:
        raise ValidationFailedError(
            f"pages of another project cannot be pinned: {', '.join(foreign)}",
            details=[f"knowledge: {p}: not in {project} or global" for p in foreign],
        )


async def _snapshot_attempt(tx: Tx, task: Task, agent_id: str) -> None:
    """Keep the definition this attempt was handed (task edit 3.5)."""
    definition = {
        f: getattr(task, f)
        for f in (
            "kind",
            "project",
            "title",
            "spec",
            "acceptance",
            "priority",
            "required_capabilities",
            "knowledge",
            "depends_on",
        )
    }
    await tx.conn.execute(
        "INSERT INTO task_attempts (task_id, attempt, agent_id, claimed_at, definition)"
        " VALUES (?, ?, ?, ?, ?)",
        (task.id, task.attempt, agent_id, ts(tx.now), json.dumps(definition)),
    )


async def attempt_definitions(
    conn: aiosqlite.Connection, task_id: str
) -> list[TaskAttempt]:
    """The definition of every attempt, oldest first."""
    await _row(conn, task_id)
    async with conn.execute(
        "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY id", (task_id,)
    ) as cur:
        return [
            TaskAttempt(
                attempt=r["attempt"],
                agent_id=r["agent_id"],
                claimed_at=r["claimed_at"],
                definition=json.loads(r["definition"]),
                outcome=r["outcome"],
                outcome_reason=r["outcome_reason"],
            )
            for r in await cur.fetchall()
        ]


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


RELEASE_REASON_MAX = 500


async def release(
    tx: Tx, task_id: str, actor: str, reason: str, *, attempt: int | None = None
) -> Task:
    """An operator takes a claimed task back from its holder: `ready` again with
    `failures` untouched, so the attempt never counts toward `max_attempts` (3.3).
    `attempt`, when given, must be the current one. The holder's account leases
    end with it unless it holds another claim; its next write gets 409."""
    from argus_api.accounts import service as accounts  # it imports this module

    row = await _row(tx.conn, task_id)
    if row["state"] != S.CLAIMED:
        raise ConflictError(
            f"task is {row['state']}, not claimed", code="task_not_claimed"
        )
    if attempt is not None and attempt != row["attempt"]:
        raise ConflictError(
            f"task {task_id} is at attempt {row['attempt']}, not {attempt}",
            code="stale_attempt",
        )
    holder = row["assignee_agent_id"]
    await release_claim(tx, row, actor, reason)
    async with tx.conn.execute(
        "SELECT 1 FROM tasks WHERE state = 'claimed' AND assignee_agent_id = ?",
        (holder,),
    ) as cur:
        if await cur.fetchone() is None:
            await accounts.release(tx, holder, "task_released")
    return await get_task(tx.conn, task_id)


async def release_agent_tasks(
    tx: Tx, agent_id: str, actor: str, reason: str
) -> list[str]:
    """Release every task an agent holds (an operator ended it: 3.4). The caller
    deals with the agent's account leases."""
    async with tx.conn.execute(
        "SELECT * FROM tasks WHERE state = 'claimed' AND assignee_agent_id = ?"
        " ORDER BY id",
        (agent_id,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        await release_claim(tx, row, actor, reason)
    return [r["id"] for r in rows]


async def release_claim(tx: Tx, row: aiosqlite.Row, actor: str, reason: str) -> None:
    """Requeue a claimed row without a failure and mark its attempt `released`."""
    await _apply(
        tx,
        row,
        Action.RELEASE,
        actor,
        set_={
            "error": None,
            "progress_message": f"attempt {row['attempt']} released: {reason};"
            " requeued",
            "progress_percent": None,
            "progress_at": ts(tx.now),
        },
        data={"reason": reason, "previous_holder": row["assignee_agent_id"]},
        event="task.released",
    )
    await tx.conn.execute(
        "UPDATE task_attempts SET outcome = 'released', outcome_reason = ?"
        " WHERE id = (SELECT max(id) FROM task_attempts"
        "  WHERE task_id = ? AND attempt = ?)",
        (reason, row["id"], row["attempt"]),
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


async def check_lease(
    tx: Tx | Snapshot,
    task_id: str,
    attempt: int,
    agent_id: str,
    *,
    allow_expired: bool = False,
) -> aiosqlite.Row:
    """Public ownership check for writes owned by other modules (artifacts, accounts).
    On a read `Snapshot` it is only a cheap early refusal; the write repeats it."""
    return await _owned(tx, task_id, attempt, agent_id, allow_expired=allow_expired)


async def _owned(
    tx: Tx | Snapshot,
    task_id: str,
    attempt: int,
    agent_id: str,
    *,
    allow_expired: bool = False,
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
    counts_failure: bool = True,
    event: str | None = None,
) -> TaskState:
    """Validate and persist one transition, its event (`task.<action>` unless
    `event` names it), and its knock-on effects.

    `counts_failure=False` evaluates a fail/expire as if no failure had counted, so it
    can never exhaust the attempts (the caller leaves `failures` unchanged)."""
    old = S(row["state"])
    ctx = TransitionContext(
        failures=row["failures"] if counts_failure else -1,
        max_attempts=row["max_attempts"],
        review=Review(row["review"]),
        deps_done=deps_done,
    )
    new = transition(old, action, ctx)
    # strictly greater than the old version, so a transition always stales a pending edit
    bumped = ts(max(tx.now, datetime.fromisoformat(row["updated_at"]) + _TICK))
    fields: dict[str, Any] = {"state": new, "updated_at": bumped, **(set_ or {})}
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
        event or f"task.{action}",
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
        await _check_pages_exist(conn, body.knowledge)


async def _check_pages_exist(conn: aiosqlite.Connection, pages: list[str]) -> None:
    found = set((await _states_of(conn, "knowledge_pages", pages)).keys())
    if missing := sorted(set(pages) - found):
        raise NotFoundError(
            f"knowledge page not found: {', '.join(missing)}",
            details=[f"knowledge: {m}" for m in missing],
        )


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
    if data["progress_data"] is not None:
        data["progress_data"] = json.loads(data["progress_data"])
    if data["account_providers"] is not None:
        data["account_providers"] = json.loads(data["account_providers"])
    data["requires_approval"] = bool(data["requires_approval"])
    data["version"] = row["updated_at"]
    data["editable_fields"] = editable_fields(
        S(row["state"]), archived=row["archived_at"] is not None
    )
    data["depends_on"] = await _deps(conn, row["id"])
    data["refill_at"] = await _refill_at(conn, row)
    data["serving_account"] = await _serving_account(conn, row)
    data["failover"] = await _last_failover(conn, row)
    data["failure_class"] = _failure_class(row, data["failover"])
    data["models"] = (await modeltrace.summaries(conn, [row["id"]]))[row["id"]]
    async with conn.execute(
        "SELECT page_id FROM task_knowledge WHERE task_id = ? ORDER BY page_id",
        (row["id"],),
    ) as cur:
        data["knowledge"] = [r[0] for r in await cur.fetchall()]
    return Task.model_validate(data)


async def _refill_at(conn: aiosqlite.Connection, row: aiosqlite.Row) -> datetime | None:
    """When the earliest exhausted account a model run could use resets."""
    if row["state"] != S.BLOCKED or row["block_reason"] not in QUOTA_WAIT_REASONS:
        return None
    return (await engine_pool(conn, None)).next_reset


async def _serving_account(
    conn: aiosqlite.Connection, row: aiosqlite.Row
) -> ServingAccount | None:
    """The account the claiming agent's harness runs on (name only, no secret)."""
    if row["state"] != S.CLAIMED:
        return None
    async with conn.execute(
        "SELECT a.id, a.name, a.provider, a.config FROM account_leases l"
        " JOIN accounts a ON a.id = l.account_id WHERE l.agent_id = ?"
        " AND l.role = 'engine' AND l.released_at IS NULL",
        (row["assignee_agent_id"],),
    ) as cur:
        acc = await cur.fetchone()
    if acc is None:
        return None
    config = CustomConfig.model_validate_json(acc["config"]) if acc["config"] else None
    return ServingAccount(
        id=acc["id"],
        name=acc["name"],
        provider=acc["provider"],
        model=config.engine.model if config else None,
        host=config.host if config else None,
    )


async def _last_failover(
    conn: aiosqlite.Connection, row: aiosqlite.Row
) -> Failover | None:
    """The last switch of the serving account, derived from events: the failover
    itself plus what the channel reported after it (applied, or failed to stop the
    old runtime). Never a credential or a prompt."""
    if row["attempt"] == 0:
        return None
    async with conn.execute(
        "SELECT id, at, data FROM events WHERE subject_type = 'task'"
        " AND subject_id = ? AND kind = 'account.failover' ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ) as cur:
        event = await cur.fetchone()
    if event is None:
        return None
    d = json.loads(event["data"])
    attempt = d.get("attempt", row["attempt"])
    async with conn.execute(
        "SELECT kind, at, data FROM events WHERE subject_type = 'task'"
        " AND subject_id = ? AND id > ? AND kind IN"
        " ('account.switch_applied', 'account.switch_failed') ORDER BY id DESC",
        (row["id"], event["id"]),
    ) as cur:
        later = [
            e
            for e in await cur.fetchall()
            if json.loads(e["data"]).get("attempt") in (attempt, attempt + 1)
        ]
    outcome = d.get("outcome")
    apply = d.get("apply")
    apply_error = None
    applied_at = None
    if later:
        last = later[0]
        if last["kind"] == "account.switch_applied":
            apply, applied_at = "applied", datetime.fromisoformat(last["at"])
        else:
            outcome, apply = "switch_failed", "failed"
            apply_error = json.loads(last["data"]).get("error")
    reset_at = d.get("reset_at")
    return Failover(
        from_account=d.get("from"),
        to_account=d.get("to"),
        reason=d.get("reason", ""),
        at=datetime.fromisoformat(event["at"]),
        attempt=attempt,
        reason_code=d.get("reason_code"),
        reset_status=d.get("reset_status"),
        reset_text=d.get("reset_text"),
        reset_at=datetime.fromisoformat(reset_at) if reset_at else None,
        source=d.get("source"),
        outcome=outcome,
        apply=apply,
        apply_error=apply_error,
        applied_at=applied_at,
    )


def _failure_class(row: aiosqlite.Row, failover: Failover | None) -> str | None:
    """Why the task last failed, for REST/MCP/UI. Only `quota_exhausted` and
    `auth_expired` rotate the account; the rest never do."""
    if row["block_reason"] == RUNTIME_SWITCH_FAILED:
        return RUNTIME_SWITCH_FAILED
    if (
        failover is not None
        and failover.reason_code
        and failover.attempt == row["attempt"]
        and row["state"] in (S.READY, S.BLOCKED)
    ):
        return failover.reason_code
    if row["error"]:
        return failure_class(row["error"])
    return None


def _deadline(now: datetime, ttl_s: int) -> str:
    return ts(now + timedelta(seconds=ttl_s))
