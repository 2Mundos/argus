"""Task operations. Every write takes an open `Tx` and records its events in it.

See docs/specs/worker-lifecycle.spec.md 3.2 and 3.3.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS, LOCAL_PROVIDER
from argus_core.agents import LIVE, AgentState
from argus_core.auth import Scope
from argus_core.continuation import (
    ChecklistItem,
    ContinuationArtifact,
    ContinuationProvenance,
    ContinuationUpdate,
    ContinuationWip,
    EffectiveContinuation,
    EnvCheck,
    ItemStatus,
    ObservationRequest,
    clean,
    effective,
    merge_acceptance,
)
from argus_core.custom_accounts import CustomConfig, serves_role
from argus_core.decisions import Audience, DecisionKind
from argus_core.difficulty import Difficulty, DifficultySource, classify, escalated
from argus_core.kinds import TaskKind
from argus_core.quota import failure_class
from argus_core.task_edit import CLEARABLE, TaskAttempt, TaskChanges, editable_fields
from argus_core.tasks import (
    TERMINAL,
    Action,
    ExecutorModel,
    Failover,
    HarnessModel,
    LeaseObservation,
    PromptTrust,
    RetryClass,
    Review,
    ServingAccount,
    StallCause,
    Task,
    TaskCreate,
    TaskResult,
    TaskState,
    TransitionContext,
    check_harness_providers,
    retry_class,
    retry_delay_s,
    transition,
    validate_milestone,
)
from pydantic import ValidationError

from argus_api import orchestrator
from argus_api.access import record_lease_grant
from argus_api.accounts.eligibility import engine_pool
from argus_api.accounts.usage import (
    ensure_attempt_outcome,
    insert_usage,
)
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
from argus_api.runtime.gate import claim_gate
from argus_api.tasks import continuation as cont
from argus_api.tasks import steers

log = logging.getLogger(__name__)
S = TaskState
_JSON_FIELDS = ("acceptance", "input", "required_capabilities")

# `blocked` for want of a usable model account (account selection 3.6). Rows written
# before the rename carry `no_account`; both are read and released alike.
AWAITING_QUOTA = "awaiting_quota"
# Admission failures end an attempt without spending it: the task never ran.
# `account_unavailable` is a lease or apply error that is not a provider limit.
ADMISSION_REASONS = frozenset({"quota", "account_unavailable"})
QUOTA_WAIT_REASONS = (AWAITING_QUOTA, "no_account")

# `blocked` while the project's image is prepared / after its preparation failed
# (project runtime spec 3.4): neither spends `failures` or `max_attempts`.
RUNTIME_PREPARING = "runtime_preparing"
RUNTIME_FAILED = "runtime_failed"
# `blocked` after the worker said FAIL with attempts left (failure classes 3.2).
WORKER_GAVE_UP = "worker_gave_up"

# Exits that end the session itself; a confirmed one follows the automatic resume
# rule (failure classes 3.2). `session_lost` is confirmed by the channel's 90 s
# listing check, `agent_ended` only by `gone` observations of the attempt that held
# without a break for as long (`SESSION_GONE_STABLE_S`).
AUTO_RESUME_REASONS = frozenset({"session_lost", "agent_ended"})
# The server's cap, in UTF-8 bytes, on an exit's `error` (the channel sends at most 2 KiB).
EXIT_ERROR_MAX = 2048
# The channel's session grace (worker lifecycle 3.8): how long `gone` must hold.
SESSION_GONE_STABLE_S = 90


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
    harness_account = await _resolve_harness_account(
        tx.conn,
        body.harness_account,
        account_providers=body.account_providers,
        harness_model=body.harness_model,
        executor_model=body.executor_model,
    )
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
    difficulty = body.difficulty or classify(body.title, body.spec)
    source = (
        body.difficulty_source or DifficultySource.DECLARED
        if body.difficulty
        else DifficultySource.INFERRED
    )
    origin_conversation_id = (
        await orchestrator.task_origin(tx.conn, tx.now, actor.removeprefix("agent:"))
        if actor.startswith("agent:")
        else None
    )

    await tx.conn.execute(
        "INSERT INTO tasks (id, parent_id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " prompt_trust, lease_ttl_s, max_attempts, project, timeout_s, created_by,"
        " created_at, updated_at, difficulty, difficulty_source, account_providers,"
        " harness_model, executor_model, harness_account, origin_conversation_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?)",
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
            difficulty,
            source,
            _providers_json(body.account_providers),
            _pin_json(body.harness_model),
            _pin_json(body.executor_model),
            harness_account,
            origin_conversation_id,
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
    can_lease: Callable[[Tx, aiosqlite.Row], Awaitable[bool]] | None = None,
) -> Task | None:
    """Claim the highest-priority, oldest ready task the agent is capable of.

    With `runtime_providers` and a fleet that manages model accounts, a task is
    only claimed when the caller can run it now: it already holds an engine lease of
    a provider the task allows, or such an account has a free lease slot (a task
    restricted to `account_providers` allows only those; local harness spec 3). A
    task nobody can serve (no free slot and no idle holder) is parked as
    `awaiting_quota` instead, without spending an attempt, and is woken when an
    account frees up (account selection 3.6); one an idle holder elsewhere can serve
    stays ready for it. `can_lease` replaces the first test with the account
    selection's own answer (the caller leases right after, in the same transaction).
    Without `runtime_providers` (or with an empty list) only
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
        "SELECT * FROM tasks t WHERE state = 'ready'"
        " AND (retry_at IS NULL OR retry_at <= ?) AND NOT EXISTS ("
        "  SELECT 1 FROM json_each(t.required_capabilities) rc"
        "  WHERE rc.value NOT IN (SELECT value FROM json_each(?)))"
        " ORDER BY priority DESC, created_at, id",
        (ts(tx.now), json.dumps(capabilities)),
    ) as cur:
        candidates = await cur.fetchall()
    gated = bool(runtime_providers) and await manages_accounts(tx.conn)
    row = None
    runtime_gates: dict[str, Any] = {}
    for candidate in candidates:
        project = candidate["project"]
        if project not in runtime_gates:
            runtime_gates[project] = await claim_gate(tx.conn, agent_id, project)
        if (blocked := runtime_gates[project]) is not None:
            # Not claimable on this host now. A failed preparation is durable and
            # visible on the task; a build in progress just leaves it ready.
            if blocked.failed:
                await _apply(
                    tx,
                    candidate,
                    Action.BLOCK,
                    f"agent:{agent_id}",
                    set_={
                        "block_reason": RUNTIME_FAILED,
                        "error": blocked.reason,
                        "progress_message": "project runtime unavailable",
                    },
                    data={"reason": RUNTIME_FAILED, "error": blocked.reason},
                )
            continue
        pinned = candidate["harness_account"]
        if (
            candidate["account_providers"] is None
            and pinned is None
            and candidate["harness_model"] is None
            and not gated
        ):
            row = candidate
            break
        choice = await engine_choice_for(
            tx.conn, tx.now, candidate, agent_id, runtime_providers
        )
        providers = (
            sorted(ENGINE_PROVIDERS) if choice.providers is None else choice.providers
        )
        if (
            await can_lease(tx, candidate)
            if can_lease is not None
            else await _restriction_met(
                tx, providers, runtime_providers, agent_id, choice.account
            )
        ):
            row = candidate
            break
        if (gated or pinned is not None) and not await can_serve(tx, candidate):
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
            "session_gone_at": None,
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
    """A trivial task ranks the local model first (task difficulty spec 3)."""
    row = await _row(conn, task_id)
    return row["difficulty"] == Difficulty.TRIVIAL


async def executor_provider(conn: aiosqlite.Connection, task_id: str) -> str | None:
    """The provider of the task's pinned executor, None when not pinned."""
    pin = (await _row(conn, task_id))["executor_model"]
    return None if pin is None else ExecutorModel.model_validate_json(pin).provider


@dataclass(frozen=True)
class EngineAccount:
    """An engine account as the pin preference sees it: `status` is `usable`
    (available or past its reset, below `max_leases` or held by the caller),
    `exhausted` (held back, or every slot leased out) or `disabled`."""

    id: str
    provider: str
    status: str


@dataclass(frozen=True)
class EngineChoice:
    """The engine role's effective provider set for one task (task models spec 3.2).
    `providers` None means every engine provider; `account` the one account it may
    use; `reason` (`exhausted`, `disabled`, `offline`) is set when a pin fell back."""

    providers: list[str] | None
    account: str | None = None
    reason: str | None = None

    def fits(self, provider: str, account_id: str) -> bool:
        return (self.providers is None or provider in self.providers) and (
            self.account is None or account_id == self.account
        )


def pinned_provider(row: aiosqlite.Row) -> str | None:
    """The provider the task's harness model pin needs, None when not pinned."""
    pin = row["harness_model"]
    return None if pin is None else HarnessModel.model_validate_json(pin).provider


def engine_choice(
    row: aiosqlite.Row,
    accounts: list[EngineAccount],
    known: set[str],
    runtime_providers: list[str] | None = None,
) -> EngineChoice:
    """The one decision of which engine providers and account a task may use now: a
    harness pin is a preference, kept while its provider (or account) is usable and
    otherwise dropped for the stored `account_providers` (task models spec 3.2-3.4).
    The claim gate, waking, lease, `exhausted` and `fail_task` all read it. A pinned
    account that no longer exists never falls back: the task waits for its edit."""
    stored = (
        None
        if row["account_providers"] is None
        else json.loads(row["account_providers"])
    )
    pin = pinned_provider(row)
    if pin is not None and stored is not None and pin not in stored:
        return EngineChoice([])  # edits refuse this; never widen if it happens anyway
    online = [
        a
        for a in accounts
        if runtime_providers is None or a.provider in runtime_providers
    ]
    reason = None
    if (account_id := row["harness_account"]) is not None:
        if account_id not in known:
            return EngineChoice([], account_id)
        mine = [a for a in accounts if a.id == account_id]
        if any(a.status == "usable" for a in online if a.id == account_id):
            return EngineChoice([mine[0].provider], account_id)
        reason = _unusable_reason(mine, online)
    if pin is not None:
        mine = [a for a in accounts if a.provider == pin]
        if any(a.status == "usable" for a in online if a.provider == pin):
            return EngineChoice([pin], None, reason)
        reason = reason or _unusable_reason(mine, online)
    return EngineChoice(stored, None, reason)


def _unusable_reason(mine: list[EngineAccount], online: list[EngineAccount]) -> str:
    """Why the pinned account(s) cannot serve: `offline` when no runtime applies
    them (or none is known), `exhausted` when any is spent or full, else `disabled`."""
    reachable = [a for a in mine if a in online]
    if not reachable:
        return "offline"
    if any(a.status == "exhausted" for a in reachable):
        return "exhausted"
    return "disabled"


async def known_engine_accounts(conn: aiosqlite.Connection) -> set[str]:
    async with conn.execute("SELECT id FROM accounts") as cur:
        return {r[0] for r in await cur.fetchall()}


async def engine_accounts(
    conn: aiosqlite.Connection, now: datetime, agent_id: str | None = None
) -> list[EngineAccount]:
    """Every engine account with its `status` for the pin preference. An account the
    caller holds counts even at `max_leases`."""
    async with conn.execute(
        "SELECT a.id, a.provider, a.config, a.state, a.exhausted_until, a.max_leases,"
        " (SELECT count(*) FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.released_at IS NULL) AS active,"
        " EXISTS (SELECT 1 FROM account_leases l WHERE l.account_id = a.id"
        "  AND l.agent_id = ? AND l.role = 'engine'"
        "  AND l.released_at IS NULL) AS mine FROM accounts a",
        (agent_id,),
    ) as cur:
        rows = await cur.fetchall()
    out = []
    for r in rows:
        config = CustomConfig.model_validate_json(r["config"]) if r["config"] else None
        if r["provider"] not in ENGINE_PROVIDERS or not serves_role(
            r["provider"], config, "engine"
        ):
            continue
        until = r["exhausted_until"] and datetime.fromisoformat(r["exhausted_until"])
        full = (
            r["max_leases"] is not None
            and r["active"] >= r["max_leases"]
            and not r["mine"]
        )
        held_back = r["state"] == "exhausted" and (not until or until > now)
        if r["state"] == "disabled":
            status = "disabled"
        else:
            status = "exhausted" if held_back or full else "usable"
        out.append(EngineAccount(r["id"], r["provider"], status))
    return out


async def engine_choice_for(
    conn: aiosqlite.Connection,
    now: datetime,
    row: aiosqlite.Row,
    agent_id: str | None,
    runtime_providers: list[str] | None,
) -> EngineChoice:
    """`engine_choice` over the registry as the caller sees it."""
    return engine_choice(
        row,
        await engine_accounts(conn, now, agent_id),
        await known_engine_accounts(conn),
        runtime_providers,
    )


async def task_row(conn: aiosqlite.Connection, task_id: str) -> aiosqlite.Row:
    return await _row(conn, task_id)


async def record_model_switch(
    tx: Tx,
    task_id: str,
    attempt: int,
    agent_id: str,
    reason: str,
    leased: dict[str, str],
) -> dict[str, Any] | None:
    """A harness pin fell back to the account the engine lease landed on (task models
    spec 3.3): a `task.model_switched` event and the attempt snapshot's `model_switch`.
    `from` is the pin (provider, model, account id), `to` the leased account (ids and
    names only; its model is the channel's policy default, unknown here). Once per
    attempt and account. Returns the record, None when already recorded."""
    row = await _row(tx.conn, task_id)
    pin = row["harness_model"] and HarnessModel.model_validate_json(
        row["harness_model"]
    )
    provider = pin.provider if pin else None
    if row["harness_account"] is not None:
        async with tx.conn.execute(
            "SELECT provider FROM accounts WHERE id = ?", (row["harness_account"],)
        ) as cur:
            found = await cur.fetchone()
        provider = found["provider"] if found else provider
    switch = {
        "from": {
            "provider": provider,
            "model": pin.model if pin else None,
            "account_id": row["harness_account"],
        },
        "to": {
            "provider": leased["provider"],
            "model": None,
            "account_id": leased["id"],
            "account_name": leased["name"],
        },
        "reason": reason,
        "attempt": attempt,
    }
    async with tx.conn.execute(
        "SELECT id, definition FROM task_attempts WHERE task_id = ? AND attempt = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, attempt),
    ) as cur:
        snapshot = await cur.fetchone()
    if snapshot is None:
        return None
    definition = json.loads(snapshot["definition"])
    if (definition.get("model_switch") or {}).get("to", {}).get("account_id") == leased[
        "id"
    ]:
        return None
    definition["model_switch"] = switch
    await tx.conn.execute(
        "UPDATE task_attempts SET definition = ? WHERE id = ?",
        (json.dumps(definition), snapshot["id"]),
    )
    await record(
        tx, f"agent:{agent_id}", "task", task_id, "task.model_switched", switch
    )
    return switch


async def attempt_model_switch(
    conn: aiosqlite.Connection, task_id: str, attempt: int
) -> dict[str, Any] | None:
    """The `model_switch` the attempt snapshot carries, None when the pin held."""
    async with conn.execute(
        "SELECT definition FROM task_attempts WHERE task_id = ? AND attempt = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, attempt),
    ) as cur:
        row = await cur.fetchone()
    return json.loads(row["definition"]).get("model_switch") if row else None


async def _restriction_met(
    tx: Tx,
    providers: list[str],
    runtime_providers: list[str] | None,
    agent_id: str | None = None,
    account_id: str | None = None,
) -> bool:
    """Can the caller start a task allowing `providers` now: the runtime applies one
    of them and either the caller already holds an engine lease of one, or an account
    of one is usable (available or past its reset, below `max_leases`; an
    unreachable local model is disabled by its probe, so it never counts). With
    `account_id` (a harness account pin) only that account counts, held or usable."""
    wanted = sorted(set(providers) & set(runtime_providers or []))
    if not wanted:
        return False
    async with tx.conn.execute(
        "SELECT EXISTS (SELECT 1 FROM account_leases l JOIN accounts a"
        "  ON a.id = l.account_id WHERE l.agent_id = ? AND l.role = 'engine'"
        "  AND l.released_at IS NULL"
        "  AND a.provider IN (SELECT value FROM json_each(?))"
        "  AND (? IS NULL OR a.id = ?))"
        " OR EXISTS (SELECT 1 FROM accounts a"
        " WHERE a.provider IN (SELECT value FROM json_each(?))"
        "  AND (? IS NULL OR a.id = ?)"
        "  AND (a.state = 'available'"
        "   OR (a.state = 'exhausted' AND a.exhausted_until <= ?))"
        "  AND (a.max_leases IS NULL OR a.max_leases >"
        "   (SELECT count(*) FROM account_leases l"
        "    WHERE l.account_id = a.id AND l.released_at IS NULL)))",
        (
            agent_id,
            json.dumps(wanted),
            account_id,
            account_id,
            json.dumps(wanted),
            account_id,
            account_id,
            ts(tx.now),
        ),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0])


async def manages_accounts(conn: aiosqlite.Connection) -> bool:
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
    """`[provider, slots, account_id]` of each usable engine account, cloud before
    the local model: how many more tasks it can serve now. Slots are the free lease slots plus
    the engine leases held by agents with no claimed task (they can take one at
    once); None means no lease limit. Accounts with no slot are left out, and so are
    accounts of a provider no live agent's runtime can apply (a `custom` account no
    worker runs is no capacity)."""
    served = await _served_providers(tx.conn)
    async with tx.conn.execute(
        "SELECT a.id, a.provider, a.max_leases,"
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
            out.append([r["provider"], None, r["id"]])
            continue
        slots = max(0, r["max_leases"] - r["active"]) + r["idle"]
        if slots > 0:
            out.append([r["provider"], slots, r["id"]])
    out.sort(key=lambda c: c[0] == LOCAL_PROVIDER)  # stable: cloud first
    return out


async def _capacity_choice(
    tx: Tx, row: aiosqlite.Row, capacity: list[list[Any]]
) -> EngineChoice:
    """`engine_choice` over the accounts that have a slot now (waking, parking)."""
    return engine_choice(
        row,
        [
            EngineAccount(c[2], c[0], "usable")
            for c in capacity
            if c[1] is None or c[1] > 0
        ],
        await known_engine_accounts(tx.conn),
    )


async def can_serve(tx: Tx, row: aiosqlite.Row) -> bool:
    """Could any agent run this task's engine role now, under its effective provider
    set (pin preference included)?"""
    capacity = await engine_capacity(tx)
    choice = await _capacity_choice(tx, row, capacity)
    return any(choice.fits(c[0], c[2]) for c in capacity)


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
        "SELECT id, account_providers, harness_model, harness_account FROM tasks"
        " WHERE state = 'blocked'"
        " AND block_reason IN (?, ?) ORDER BY priority DESC, created_at, id",
        QUOTA_WAIT_REASONS,
    ) as cur:
        rows = await cur.fetchall()
    woken: list[str] = []
    for r in rows:
        choice = await _capacity_choice(tx, r, capacity)
        slot = next(
            (
                c
                for c in capacity
                if (c[1] is None or c[1] > 0) and choice.fits(c[0], c[2])
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


Lease = (
    tuple[str, int]
    | tuple[str, int, datetime | None]
    | tuple[str, int, datetime | None, LeaseObservation | None]
)


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
    tx: Tx,
    agent_id: str,
    leases: list[Lease],
) -> tuple[list[RenewedLease], list[RejectedLease]]:
    """Extend each still-valid lease; a rejected pair never blocks the others.

    An optional third element is `activity_at` (stall visibility): clamped to
    `<= tx.now`, never moved backwards, and clears `quiet_at` when it advances.
    An optional fourth is the host's `LeaseObservation` (3.7), stored with
    `observation_at = tx.now`; it never renews, expires or clears anything. A
    `gone` session starts or extends `session_gone_at`; any other observation, or
    a renewal without one, breaks that run (failure classes 3.2)."""
    renewed: list[RenewedLease] = []
    rejected: list[RejectedLease] = []
    for item in leases:
        task_id, attempt = item[0], item[1]
        activity_at = item[2] if len(item) > 2 else None
        observation = item[3] if len(item) > 3 else None
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
        if activity_at is not None:
            await _record_activity(tx, task_id, row, activity_at)
        if observation is not None:
            # observation_at is this transaction's time, so it is never ahead of
            # tx.now; an older write (a skewed clock) never replaces a newer one.
            await tx.conn.execute(
                "UPDATE tasks SET observation = ?, observation_at = ?,"
                " session_gone_at = CASE WHEN ? THEN COALESCE(session_gone_at, ?)"
                " END WHERE id = ? AND (observation_at IS NULL OR observation_at <= ?)",
                (
                    observation.model_dump_json(),
                    ts(tx.now),
                    observation.session == "gone",
                    ts(tx.now),
                    task_id,
                    ts(tx.now),
                ),
            )
        else:
            await tx.conn.execute(
                "UPDATE tasks SET session_gone_at = NULL WHERE id = ?", (task_id,)
            )
        renewed.append(RenewedLease(task_id, attempt, deadline))
    return renewed, rejected


async def _record_activity(
    tx: Tx, task_id: str, row: aiosqlite.Row, activity_at: datetime
) -> None:
    """Store a newer activity signal and clear quiet when it advances."""
    at = min(activity_at, tx.now)
    previous = row["activity_at"]
    if previous is not None and at <= datetime.fromisoformat(previous):
        return
    await tx.conn.execute(
        "UPDATE tasks SET activity_at = ?, quiet_at = NULL WHERE id = ?",
        (ts(at), task_id),
    )


async def complete(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    result: TaskResult,
    *,
    model_policy_path: Path | None = None,
) -> Task:
    """Complete with the result envelope; `data` must match the kind's result
    schema and every listed artifact must belong to this task (3.11).

    When `review.independent` is on for the project and the task delivers code,
    completion moves it to `in_review` and spawns a `code_review` child instead
    of succeeding (settings.spec.md 3.5)."""
    from argus_api.settings import service as settings_svc
    from argus_api.tasks import independent_review as indep

    row = await _owned(tx, task_id, attempt, agent_id)
    kind = await kinds.get_kind(tx.conn, row["kind"])
    kinds.validate(kind.result_schema, result.data, "result.data")
    await _check_artifacts(tx.conn, task_id, result.artifacts)
    have = cont.load(row)
    held = merge_acceptance(have.checklist, json.loads(row["acceptance"]))
    # A report naming an item this task does not hold came from another task's work
    # (a late reply after a switch); fail closed instead of recording it as evidence.
    foreign = (
        sorted({r.id for r in result.checklist} - {i.id for i in held}) if held else []
    )
    if foreign:
        raise ValidationFailedError(
            "checklist items do not belong to this task",
            code="checklist_mismatch",
            details=[f"unknown checklist item {i}" for i in foreign],
        )
    await _record_result_usage(tx, agent_id, task_id, attempt, result)
    # A completion claims; only evidenced items count as done. Anything still open
    # sends the result to review instead of success (continuation spec 3.5).
    items = cont.apply_reports(
        held,
        result.checklist,
        ContinuationProvenance.WORKER,
        tx.now,
    )
    await cont.store(
        tx,
        task_id,
        have,
        have.model_copy(update={"checklist": items}),
        f"agent:{agent_id}",
        "complete",
    )
    open_items = [i.id for i in items if i.status is not ItemStatus.DONE]
    if not items and (row["attempt"] > 1 or have.artifacts or have.wip):
        # Continued work whose criteria Argus never held: nothing proves them, so the
        # result is reviewed (assisted reconciliation) instead of auto-accepted.
        open_items = ["acceptance_not_structured"]

    actor = f"agent:{agent_id}"
    path = model_policy_path or Path("data/model-policy.json")
    review_setting = await settings_svc.get_review(tx.conn, path, row["project"])
    policy, _ = await settings_svc.effective_model_policy(tx.conn, path, row["project"])

    # Child code_review completion drives the parent's independent-review gate.
    if row["kind"] == indep.CODE_REVIEW_KIND and row["parent_id"]:
        await _apply(
            tx,
            row,
            Action.COMPLETE,
            actor,
            set_={"result": result.model_dump_json(exclude_none=True)},
        )
        await indep.on_review_child_complete(
            tx,
            await _row(tx.conn, task_id),
            result,
            setting=review_setting,
            policy=policy,
            actor=actor,
        )
        return await get_task(tx.conn, task_id)

    if await indep.on_parent_complete(
        tx,
        row,
        result,
        setting=review_setting,
        policy=policy,
        actor=actor,
    ):
        return await get_task(tx.conn, task_id)

    await _apply(
        tx,
        row,
        Action.COMPLETE,
        actor,
        set_={"result": result.model_dump_json(exclude_none=True)},
        data={"acceptance_open": open_items} if open_items else None,
        review=Review.ORCHESTRATOR
        if open_items and Review(row["review"]) is Review.NONE
        else None,
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
        " lease_expires_at = ?, quiet_at = NULL WHERE id = ?",
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
        await cont.on_milestone(tx, row, attempt, kind, data or {}, f"agent:{agent_id}")
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

    Reasons `quota` (the provider's limit) and `account_unavailable` (no account
    could be leased or applied) are not the task's fault and not a failure:
    `failures` stays. The task returns to ready when some agent can run it now (a
    free account slot, or an idle agent holding a fitting lease); otherwise it waits
    as `awaiting_quota` until `wake_waiting` frees it, instead of bouncing between
    agents that cannot lease an account (account selection 3.6)."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    quota = reason in ADMISSION_REASONS
    if quota and await manages_accounts(tx.conn) and not await can_serve(tx, row):
        await _apply(
            tx,
            row,
            Action.BLOCK,
            f"agent:{agent_id}",
            set_={
                "error": error,
                "block_reason": AWAITING_QUOTA,
                "progress_message": f"waiting for an account: {error}",
            },
            data={"error": error, "reason": reason},
        )
        return await get_task(tx.conn, task_id)
    harder = {} if quota else _escalation(row, reason)
    failures = row["failures"] + 1
    retries = failures < row["max_attempts"]
    kind = retry_class(reason)
    if reason == "session_lost":
        # The channel saw the container gone for 90 s: a confirmed exit.
        error = _cap_utf8(error, EXIT_ERROR_MAX)
        resume = await _auto_resume(tx, row, retries=retries)
        await _apply(
            tx,
            row,
            Action.FAIL,
            f"agent:{agent_id}",
            set_={"error": error, "failures": failures} | harder.get("set", {}),
            data={"error": error, "reason": reason, "retry_class": kind}
            | resume.data
            | harder.get("data", {}),
            exhausted=resume.exhausted,
        )
        if resume.resumed:
            await _record_auto_resumed(tx, row, f"agent:{agent_id}", reason)
        return await get_task(tx.conn, task_id)
    if kind is RetryClass.TASK and reason not in ESCALATING_REASONS and retries:
        # The worker gave up: asking again would get the same answer, so a human
        # decides (failure classes 3.2). The attempt still counts.
        await _apply(
            tx,
            row,
            Action.BLOCK,
            f"agent:{agent_id}",
            set_={"error": error, "failures": failures, "block_reason": WORKER_GAVE_UP},
            data={"error": error, "reason": reason, "retry_class": kind},
        )
        return await get_task(tx.conn, task_id)
    backoff = (
        {"retry_at": ts(tx.now + timedelta(seconds=retry_delay_s(failures)))}
        if kind is RetryClass.TRANSIENT and retries
        else {}
    )
    await _apply(
        tx,
        row,
        Action.FAIL,
        f"agent:{agent_id}",
        set_={"error": error}
        | (
            {"progress_message": f"no account for the attempt; requeued: {error}"}
            if quota
            else {"failures": failures}
        )
        | backoff
        | harder.get("set", {}),
        data={"error": error, "reason": reason, "retry_class": kind}
        | backoff
        | harder.get("data", {}),
        counts_failure=not quota,
    )
    return await get_task(tx.conn, task_id)


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


async def block(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    reason: str,
    error: str | None = None,
) -> Task:
    row = await _owned(tx, task_id, attempt, agent_id)
    await _apply(
        tx,
        row,
        Action.BLOCK,
        f"agent:{agent_id}",
        set_={"block_reason": reason} | ({"error": error} if error else {}),
        data={"reason": reason, "retry_class": retry_class(reason)}
        | ({"error": error} if error else {}),
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


async def hold_for_runtime(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    reason: str,
    error: str | None,
) -> Task:
    """The project's image is not usable for this claim: block the task before any
    model work, `attempt` and `failures` as they were. `runtime_preparing` is woken
    by the build; `runtime_failed` carries the durable `error` and waits for a
    changed recipe or an operator rebuild."""
    row = await _owned(tx, task_id, attempt, agent_id, allow_expired=True)
    note = (
        "waiting for the project runtime image"
        if reason == RUNTIME_PREPARING
        else f"project runtime unavailable: {error}"
    )
    await _apply(
        tx,
        row,
        Action.BLOCK,
        f"agent:{agent_id}",
        set_={
            "block_reason": reason,
            **({"error": error} if error else {}),
            "progress_message": note[:500],
            "progress_percent": None,
            "progress_at": ts(tx.now),
        },
        data={"reason": reason, **({"error": error} if error else {})},
    )
    return await get_task(tx.conn, task_id)


async def runtime_holds(
    tx: Tx, project: str, reasons: tuple[str, ...]
) -> list[aiosqlite.Row]:
    marks = ", ".join("?" for _ in reasons)
    async with tx.conn.execute(
        "SELECT * FROM tasks WHERE state = 'blocked' AND project = ?"  # noqa: S608 - placeholders only
        f" AND block_reason IN ({marks}) ORDER BY priority DESC, created_at, id",
        (project, *reasons),
    ) as cur:
        return list(await cur.fetchall())


async def release_runtime_holds(
    tx: Tx, project: str, reasons: tuple[str, ...], actor: str = "system"
) -> list[str]:
    """Return the project's runtime-held tasks to `ready`; the claim gate decides
    again. Nothing is claimed here, so no storm."""
    rows = await runtime_holds(tx, project, reasons)
    for r in rows:
        await unblock(tx, r["id"], actor)
    return [r["id"] for r in rows]


async def fail_runtime_holds(tx: Tx, project: str, error: str) -> list[str]:
    """The preparation the tasks waited for failed: keep them blocked, now durably
    and with the reason (no state change, so no attempt or failure is spent)."""
    rows = await runtime_holds(tx, project, (RUNTIME_PREPARING,))
    for r in rows:
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = ?, error = ?, progress_message = ?,"
            " updated_at = ? WHERE id = ?",
            (
                RUNTIME_FAILED,
                error,
                "project runtime unavailable",
                ts(max(tx.now, datetime.fromisoformat(r["updated_at"]) + _TICK)),
                r["id"],
            ),
        )
        await record(
            tx, "system", "task", r["id"], "task.runtime_failed", {"error": error[:500]}
        )
    return [r["id"] for r in rows]


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
    await tx.conn.execute("DELETE FROM task_changes WHERE task_id = ?", (task_id,))
    await steers.expire_task(tx, task_id)
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
    if new.get("harness_account") is not None:  # a name is stored as its id
        new["harness_account"] = await _account_id(tx.conn, new["harness_account"])
    changed = {f: v for f, v in new.items() if v != getattr(current, f)}
    if not changed:
        return current  # nothing differs: no write, same version
    if why := check_harness_providers(
        new.get("harness_model", current.harness_model),
        new.get("account_providers", current.account_providers),
    ):
        raise ValidationFailedError(
            f"invalid values: harness_model: {why}",
            details=[f"harness_model: {why}"],
        )
    if changed.keys() & set(_ACCOUNT_PIN_RULES):
        await _resolve_harness_account(
            tx.conn,
            new.get("harness_account", current.harness_account),
            account_providers=new.get("account_providers", current.account_providers),
            harness_model=new.get("harness_model", current.harness_model),
            executor_model=new.get("executor_model", current.executor_model),
        )

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
        if f not in ("knowledge", "depends_on", "account_providers", *_PIN_FIELDS)
    }
    if "difficulty" in sets:
        sets["difficulty_source"] = DifficultySource.DECLARED
    if "account_providers" in changed:
        sets["account_providers"] = _providers_json(changed["account_providers"])
    for f in _PIN_FIELDS:
        if f in changed:
            sets[f] = _pin_json(changed[f])
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
            if field in ("account_providers", "harness_account", *_PIN_FIELDS):
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


_PIN_FIELDS = ("harness_model", "executor_model")
# The fields a harness account pin is checked against (task models spec 3.4).
_ACCOUNT_PIN_RULES = (
    "harness_account",
    "account_providers",
    "harness_model",
    "executor_model",
)


async def _account_id(conn: aiosqlite.Connection, ref: str) -> str:
    """The id of the account `ref` names (its id or its name); 422 when none does."""
    return (await _named_account(conn, ref))["id"]


async def _named_account(conn: aiosqlite.Connection, ref: str) -> aiosqlite.Row:
    """The account `ref` names (its id first, else its name); 422 when none does."""
    async with conn.execute(
        "SELECT id, name, provider FROM accounts WHERE id = ? OR name = ?"
        " ORDER BY id = ? DESC LIMIT 1",
        (ref, ref, ref),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        why = f"no account {ref!r}"
        raise ValidationFailedError(
            f"invalid values: harness_account: {why}",
            details=[f"harness_account: {why}"],
        )
    return row


async def _resolve_harness_account(
    conn: aiosqlite.Connection,
    ref: str | None,
    *,
    account_providers: list[str] | None,
    harness_model: HarnessModel | None,
    executor_model: ExecutorModel | None,
) -> str | None:
    """The id of the account a harness account pin names, checked against the
    task's other fields (task models spec 3.4); None when the task is not pinned.
    The account must be an engine account its `account_providers` and harness pin
    allow; a local (Strata) harness only orchestrates, so its executor pin may only
    be the Cursor agent."""
    if ref is None:
        return None
    acc = await _named_account(conn, ref)
    account_id, name, provider = acc["id"], acc["name"], acc["provider"]
    why = None
    if provider not in ENGINE_PROVIDERS:
        why = f"account {name} ({provider}) cannot run a harness"
    elif account_providers is not None and provider not in account_providers:
        why = f"account {name} is {provider}, excluded by account_providers"
    elif harness_model is not None and harness_model.provider != provider:
        why = (
            f"account {name} is {provider}; harness_model engine"
            f" {harness_model.engine} needs {harness_model.provider}"
        )
    elif (
        provider == LOCAL_PROVIDER
        and executor_model is not None
        and executor_model.executor != "agent"
    ):
        why = (
            f"account {name} is the local harness, which delegates only to the"
            f" Cursor agent; executor_model {executor_model.executor} cannot run"
        )
    if why:
        raise ValidationFailedError(
            f"invalid values: harness_account: {why}",
            details=[f"harness_account: {why}"],
        )
    return account_id


def _pin_json(pin: HarnessModel | ExecutorModel | None) -> str | None:
    return None if pin is None else pin.model_dump_json()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _diff(field: str, old: Any, new: Any) -> dict[str, Any]:
    """What the audit keeps of one change. Spec and acceptance text never enter the
    event log (it is streamed and shown): only sizes and digests do."""
    if field in _PIN_FIELDS:
        return {
            "from": old and old.model_dump(),
            "to": new and new.model_dump(),
        }
    if field in (
        "title",
        "priority",
        "difficulty",
        "account_providers",
        "harness_account",
    ):
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
            "difficulty",
            "difficulty_source",
            "harness_account",
        )
    }
    for f in _PIN_FIELDS:
        pin = getattr(task, f)
        definition[f] = pin and pin.model_dump()
    # What the attempt was told about continuing (history is never rewritten).
    definition["continuation"] = (
        task.continuation.model_dump(mode="json") if task.continuation else None
    )
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
    tx: Tx,
    task_id: str,
    question: str,
    answer: str,
    actor: str,
    *,
    decision_id: str | None = None,
    choice: str | None = None,
    text: str | None = None,
    options: list[str] | None = None,
    category: str = "decision",
) -> None:
    """Append an answered question to the task spec (audit narrative the next claim
    sees as history) and record it as an effective decision with scope and time. The
    structured decision, not this text, is what a later attempt treats as current."""
    row = await _row(tx.conn, task_id)
    spec = row["spec"]
    if "\n## Clarifications\n" not in f"\n{spec}":
        spec += "\n\n## Clarifications\n"
    label = "Assistance" if category == "assistance" else "Answered"
    spec += f"\n- **Asked (attempt {row['attempt']}):** {question}\n"
    spec += f"  **{label} by {actor} ({ts(tx.now)}):** {answer}\n"
    await tx.conn.execute(
        "UPDATE tasks SET spec = ?, updated_at = ? WHERE id = ?",
        (spec, ts(tx.now), task_id),
    )
    if decision_id is not None:
        await cont.on_decision(
            tx,
            task_id,
            decision_id=decision_id,
            question=question,
            choice=choice,
            text=text if text is not None else answer,
            options=options,
            category=category,
            actor=actor,
            attempt=row["attempt"],
        )


async def answered_equivalent(tx: Tx, task_id: str, question: str):
    """An effective earlier answer to this very question, if any."""
    return await cont.answered_equivalent(tx, task_id, question)


async def continuation_of(
    conn: aiosqlite.Connection, task_id: str
) -> EffectiveContinuation:
    """The effective contract of a task (stored facts plus derived view)."""
    task = await get_task(conn, task_id)
    assert task.continuation is not None
    return task.continuation


def _artifact(
    a, provenance: ContinuationProvenance, at: datetime, attempt: int | None
) -> ContinuationArtifact:
    return ContinuationArtifact(
        **a.model_dump(exclude={"note"}),
        note=clean(a.note, 1000),
        provenance=provenance,
        observed_at=at,
        attempt=attempt,
    )


async def observe(
    tx: Tx, agent_id: str, task_id: str, body: ObservationRequest
) -> Task:
    """The holder's runtime reports what it saw of the live system (PR state, head,
    environment readiness). The runtime made those calls before this transaction; here
    they are only stored. Idempotent: repeating an observation changes nothing, and an
    older one never overwrites a newer. Contradictions with the record become bounded
    `reconciliation` entries instead of silent overwrites."""
    row = await _owned(tx, task_id, body.attempt, agent_id)
    have = cont.load(row)
    seen = [
        _artifact(a, ContinuationProvenance.RUNTIME, tx.now, body.attempt)
        for a in body.artifacts
    ]
    artifacts = have.artifacts
    for a in seen:
        artifacts = cont.upsert_artifact(artifacts, a)
    wip = have.wip
    if body.wip is not None:
        # A required input is the orchestrator's to name or replace (task inputs 3.6).
        if any(
            (w.ref or "").startswith(cont.INPUT_REF_PREFIX)
            for w in (body.wip, have.wip)
            if w is not None
        ):
            raise ValidationFailedError(
                "only an orchestrator or admin may set or replace a required input",
                code="input_ref_forbidden",
            )
        wip = ContinuationWip(
            summary=clean(body.wip.summary, 1000) or "",
            ref=clean(body.wip.ref, 250),
            provenance=ContinuationProvenance.WORKER,
            observed_at=tx.now,
        )
    env = cont.apply_environment(
        have.environment,
        [
            EnvCheck(**e.model_dump(), provenance=ContinuationProvenance.RUNTIME)
            for e in body.environment
        ],
        tx.now,
    )
    want = have.model_copy(
        update={
            "artifacts": artifacts,
            "wip": wip,
            "environment": env,
            "reconciliation": cont.reconcile(have, seen, tx.now),
        }
    )
    await cont.store(tx, task_id, have, want, f"agent:{agent_id}", "observation")
    return await get_task(tx.conn, task_id)


async def update_continuation(
    tx: Tx, task_id: str, body: ContinuationUpdate, actor: str
) -> Task:
    """An orchestrator/admin settles facts the runtime cannot: the attempt's phase,
    known artifacts, preserved WIP, checklist items and evidence, environment status,
    or a decision to retire. Fenced by `expected_revision`; audited by event."""
    row = await _row(tx.conn, task_id)
    have = cont.load(row)
    cont.fence(have, body.expected_revision)
    at = tx.now
    artifacts = have.artifacts
    for a in body.artifacts:
        artifacts = cont.upsert_artifact(
            artifacts, _artifact(a, ContinuationProvenance.ORCHESTRATOR, at, None)
        )
    items = merge_acceptance(have.checklist, json.loads(row["acceptance"]))
    by_id = {i.id: i for i in items}
    for it in body.checklist:
        if it.id is None:
            if not it.text:
                raise ValidationFailedError("a new checklist item needs text")
            new_id = "or-" + _digest(it.text)[:10]
            by_id[new_id] = ChecklistItem(
                id=new_id,
                text=clean(it.text, 1000) or "",
                source="orchestrator",
                provenance=ContinuationProvenance.ORCHESTRATOR,
                status=it.status or ItemStatus.PENDING,
                evidence=clean(it.evidence, 2000),
                updated_at=at,
            )
        elif it.id in by_id:
            cur = by_id[it.id]
            by_id[it.id] = cur.model_copy(
                update={
                    "status": it.status or cur.status,
                    "evidence": clean(it.evidence, 2000)
                    if it.evidence is not None
                    else cur.evidence,
                    "provenance": ContinuationProvenance.ORCHESTRATOR,
                    "updated_at": at,
                }
            )
        else:
            raise ValidationFailedError(f"unknown checklist item {it.id}")
    wip = have.wip
    if body.clear_wip:
        wip = None
    if body.wip is not None:
        if (body.wip.ref or "").startswith(cont.INPUT_REF_PREFIX):
            await cont.check_input_ref(tx, row, body.wip.ref or "")
        wip = ContinuationWip(
            summary=clean(body.wip.summary, 1000) or "",
            ref=clean(body.wip.ref, 250),
            provenance=ContinuationProvenance.ORCHESTRATOR,
            observed_at=at,
        )
    known = {d.decision_id for d in have.decisions}
    if unknown := sorted(set(body.supersede_decisions) - known):
        raise ValidationFailedError(f"unknown decisions: {', '.join(unknown)}")
    decisions = [
        d.model_copy(update={"status": "superseded"})
        if d.decision_id in body.supersede_decisions
        else d
        for d in have.decisions
    ]
    want = have.model_copy(
        update={
            "phase": None if body.clear_phase else (body.phase or have.phase),
            "objective": clean(body.objective, 1000)
            if body.objective is not None
            else have.objective,
            "artifacts": artifacts,
            "wip": wip,
            "checklist": list(by_id.values()),
            "environment": cont.apply_environment(
                have.environment,
                [
                    EnvCheck(
                        **e.model_dump(), provenance=ContinuationProvenance.ORCHESTRATOR
                    )
                    for e in body.environment
                ],
                at,
            ),
            "decisions": decisions,
            "reconciliation": [] if body.clear_reconciliation else have.reconciliation,
        }
    )
    await cont.store(
        tx, task_id, have, want, actor, clean(body.reason, 300) or "update"
    )
    return await get_task(tx.conn, task_id)


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
        harder = _escalation(row, "verification_failed")
        await _apply(
            tx,
            row,
            Action.REJECT,
            actor,
            set_={"spec": spec} | harder.get("set", {}),
            data={"notes": notes} | harder.get("data", {}),
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


async def expire_stalled(
    tx: Tx, stall_after_s: int, failed: list[str] | None = None
) -> list[str]:
    """Expire flagged-quiet attempts whose last signal (progress, live log,
    activity, claim) is older than `stall_after_s`, however healthy their
    heartbeats. A session that stopped working but still answers its lease would
    otherwise hold the task until the wall-clock timeout. Counts as a failure.
    0 disables; the attempt must already be flagged quiet, so the inbox warned first."""
    if stall_after_s <= 0:
        return []
    cutoff = tx.now - timedelta(seconds=stall_after_s)
    return await _expire_where(
        tx,
        "quiet_at IS NOT NULL AND ? IS NOT NULL",
        ts(tx.now),
        "stalled",
        failed,
        silent_before=cutoff,
    )


async def expire_agent_leases(tx: Tx, agent_id: str) -> list[str]:
    """Expire every lease an agent holds, e.g. because it was lost or left.

    All or nothing: the caller ends the agent in the same savepoint."""
    return await _expire_where(tx, "assignee_agent_id = ?", agent_id, "agent_ended")


# Reaper interval is 10 s (reaper.REAP_INTERVAL_S); three missed passes.
LATE_DETECT_AFTER_S = 30
QUIET_CLAIM_S = 1200  # default; reaper may pass Settings.quiet_claim_s
# Three heartbeat intervals (agents.service.HEARTBEAT_INTERVAL_S = 30): an older
# observation says nothing about now (3.7).
OBSERVATION_STALE_S = 90


def stall_cause(
    now: datetime,
    observation: str | None,
    observation_at: str | None,
    claimed_at: str | None = None,
) -> tuple[StallCause, str | None]:
    """Why a claim is silent, from the host's newest observation (3.7), with the
    observation time. Absent, stale (older than three heartbeats) or from before
    this attempt's claim: `unknown`. Descriptive only: no decision reads it."""
    if observation is None or observation_at is None:
        return "unknown", None
    at = datetime.fromisoformat(observation_at)
    if (now - at).total_seconds() > OBSERVATION_STALE_S or (
        claimed_at is not None and at < datetime.fromisoformat(claimed_at)
    ):
        return "unknown", observation_at
    seen = LeaseObservation.model_validate_json(observation)
    if seen.workspace == "missing":
        return "workspace_missing", observation_at
    if seen.session == "gone":
        return "session_gone", observation_at
    if seen.delegate == "active":
        return "silent_delegate_active", observation_at
    if (seen.workspace, seen.session, seen.delegate) == ("ok", "running", "silent"):
        return "no_progress", observation_at
    return "unknown", observation_at


def _stall_cause(tx: Tx, row: aiosqlite.Row) -> tuple[StallCause, str | None]:
    return stall_cause(
        tx.now, row["observation"], row["observation_at"], row["claimed_at"]
    )


_QUIET_SIGNALS = (
    "SELECT t.id, t.quiet_at, t.activity_at, t.progress_at, t.output_at, a.claimed_at,"
    " t.observation, t.observation_at, t.state"
    " FROM tasks t"
    " LEFT JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
)


class QuietFacts(NamedTuple):
    quiet_at: datetime | None
    last_signal_at: datetime | None
    last_signal_source: str | None
    stall_cause: StallCause | None


def quiet_facts_from_row(row: aiosqlite.Row | None, now: datetime) -> QuietFacts:
    """Quiet facts exist only while the task is claimed."""
    if row is None:
        return QuietFacts(None, None, None, None)
    claimed = row["state"] == "claimed"
    quiet_at = (
        datetime.fromisoformat(row["quiet_at"])
        if claimed and row["quiet_at"] is not None
        else None
    )
    last_at: datetime | None = None
    last_source: str | None = None
    if claimed:
        last_at, last_source = last_signal(
            activity_at=row["activity_at"],
            progress_at=row["progress_at"],
            output_at=row["output_at"],
            claimed_at=row["claimed_at"],
        )
    stall: StallCause | None = None
    if quiet_at is not None:
        stall = stall_cause(
            now, row["observation"], row["observation_at"], row["claimed_at"]
        )[0]
    return QuietFacts(quiet_at, last_at, last_source, stall)


async def quiet_facts(
    conn: aiosqlite.Connection, task_id: str, now: datetime
) -> QuietFacts:
    async with conn.execute(f"{_QUIET_SIGNALS} WHERE t.id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return quiet_facts_from_row(row, now)


async def quiet_facts_for(
    conn: aiosqlite.Connection, task_ids: list[str], now: datetime
) -> dict[str, QuietFacts]:
    """Quiet facts for many tasks in one query; ids with no row are absent."""
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    async with conn.execute(
        f"{_QUIET_SIGNALS} WHERE t.id IN ({marks})", task_ids
    ) as cur:
        rows = await cur.fetchall()
    return {r["id"]: quiet_facts_from_row(r, now) for r in rows}


def last_signal(
    *,
    activity_at: str | None = None,
    progress_at: str | None = None,
    output_at: str | None = None,
    claimed_at: str | None = None,
) -> tuple[datetime | None, str | None]:
    """Newest of activity / progress / live log / claim time, with its source."""
    candidates: list[tuple[datetime, str]] = []
    for value, source in (
        (activity_at, "activity"),
        (progress_at, "progress"),
        (output_at, "log"),
    ):
        if value is not None:
            candidates.append((datetime.fromisoformat(value), source))
    if claimed_at is not None:
        candidates.append((datetime.fromisoformat(claimed_at), "claim"))
    if not candidates:
        return None, None
    at, source = max(candidates, key=lambda c: c[0])
    return at, source


def _last_signal(
    row: aiosqlite.Row, *, claimed_at: str | None = None
) -> tuple[datetime | None, str | None]:
    return last_signal(
        activity_at=row["activity_at"],
        progress_at=row["progress_at"],
        output_at=row["output_at"],
        claimed_at=claimed_at,
    )


def _expiry_error(
    row: aiosqlite.Row,
    reason: str,
    *,
    last_signal_at: datetime | None = None,
    last_signal_source: str | None = None,
    detected_late_s: int | None = None,
    cause: StallCause = "unknown",
) -> str:
    holder = row["assignee_agent_id"]
    match reason:
        case "lease_expired":
            base = f"lease expired at {row['lease_expires_at']} (holder {holder})"
        case "timeout":
            base = f"attempt timed out at {row['timeout_at']} (holder {holder})"
        case "stalled":
            base = f"attempt stalled: no progress, output or activity (holder {holder})"
        case _:
            base = f"holder {holder} ended; its lease was released"
    if last_signal_at is not None and last_signal_source is not None:
        base += f"; last signal {last_signal_source} at {ts(last_signal_at)}"
    if reason == "stalled" or cause != "unknown":
        base += f"; stall cause: {cause.replace('_', ' ')}"
    if detected_late_s is not None and detected_late_s > 0:
        mins = max(1, round(detected_late_s / 60))
        base += f"; detected {mins} min late (Argus was not running)"
    return base


def _detected_late_s(tx: Tx, deadline: str | None) -> int | None:
    if deadline is None:
        return None
    late = int((tx.now - datetime.fromisoformat(deadline)).total_seconds())
    return late if late > LATE_DETECT_AFTER_S else None


async def flag_quiet(
    tx: Tx, quiet_after_s: int = QUIET_CLAIM_S, failed: list[str] | None = None
) -> list[str]:
    """Flag claimed tasks with no signal for `quiet_after_s` (stall visibility).

    Sets `quiet_at` once per silence and emits `task.quiet`. Never expires."""
    async with tx.conn.execute(
        "SELECT t.*, a.claimed_at FROM tasks t"
        " JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
        " WHERE t.state = 'claimed' AND t.quiet_at IS NULL ORDER BY t.id"
    ) as cur:
        rows = await cur.fetchall()
    quieted: list[str] = []
    cutoff = tx.now - timedelta(seconds=quiet_after_s)
    for row in rows:
        last_at, source = _last_signal(row, claimed_at=row["claimed_at"])
        if last_at is None or last_at > cutoff:
            continue
        cause, observed_at = _stall_cause(tx, row)
        try:
            async with tx.savepoint():
                await tx.conn.execute(
                    "UPDATE tasks SET quiet_at = ? WHERE id = ? AND quiet_at IS NULL",
                    (ts(tx.now), row["id"]),
                )
                await record(
                    tx,
                    "system",
                    "task",
                    row["id"],
                    "task.quiet",
                    {
                        "attempt": row["attempt"],
                        "agent_id": row["assignee_agent_id"],
                        "last_signal_at": ts(last_at),
                        "last_signal_source": source,
                        "stall_cause": cause,
                        "observation_at": observed_at,
                    },
                )
        except Exception:
            if failed is None:
                raise
            log.exception("could not flag quiet task %s", row["id"])
            failed.append(row["id"])
            continue
        quieted.append(row["id"])
    return quieted


async def _expire_where(
    tx: Tx,
    condition: str,
    param: str,
    reason: str,
    failed: list[str] | None = None,
    *,
    silent_before: datetime | None = None,
) -> list[str]:
    """Expire the claimed tasks matching `condition`; with `silent_before` only
    those whose last signal is older than it."""
    async with tx.conn.execute(
        f"SELECT t.*, a.claimed_at FROM tasks t"  # noqa: S608
        f" LEFT JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
        f" WHERE t.state = 'claimed' AND {condition} ORDER BY t.id",
        (param,),
    ) as cur:
        rows = await cur.fetchall()
    expired: list[str] = []
    for row in rows:
        last_at, source = _last_signal(row, claimed_at=row["claimed_at"])
        if silent_before is not None and (last_at is None or last_at > silent_before):
            continue
        deadline = {
            "lease_expired": row["lease_expires_at"],
            "timeout": row["timeout_at"],
        }.get(reason)
        late = _detected_late_s(tx, deadline)
        cause, observed_at = _stall_cause(tx, row)
        error = _expiry_error(
            row,
            reason,
            last_signal_at=last_at,
            last_signal_source=source,
            detected_late_s=late,
            cause=cause,
        )
        data: dict[str, Any] = {
            "agent_id": row["assignee_agent_id"],
            "reason": reason,
            "error": error,
            "stall_cause": cause,
            "observation_at": observed_at,
        }
        if last_at is not None:
            data["last_signal_at"] = ts(last_at)
            data["last_signal_source"] = source
        if late is not None:
            data["detected_late_s"] = late
        retries = row["failures"] + 1 < row["max_attempts"]
        try:
            async with tx.savepoint():
                resume = (
                    await _auto_resume(tx, row, retries=retries)
                    if reason in AUTO_RESUME_REASONS and _exit_confirmed(row)
                    else _NOT_CONFIRMED
                )
                data |= resume.data
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
                            f" {'requeued' if retries and not resume.exhausted else 'failed'}"
                        ),
                        "progress_percent": None,
                        "quiet_at": None,
                        "activity_at": None,
                        "observation": None,
                        "observation_at": None,
                        "session_gone_at": None,
                    },
                    data=data,
                    exhausted=resume.exhausted,
                )
                if resume.resumed:
                    await _record_auto_resumed(tx, row, "system", reason)
        except Exception:
            if failed is None:
                raise
            log.exception("could not expire task %s (%s)", row["id"], reason)
            failed.append(row["id"])
            continue
        expired.append(row["id"])
    return expired


# -- internals ------------------------------------------------------------------


def _cap_utf8(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes of `text`, never splitting a character."""
    return text.encode()[:limit].decode(errors="ignore")


class _Resume(NamedTuple):
    """The automatic resume rule's verdict on one exit (failure classes 3.2)."""

    resumed: bool  # requeued under the rule: record `task.auto_resumed`
    exhausted: bool  # no progress since the last resume: failed whatever is left
    data: dict[str, Any]  # added to the fail / expire event


_NOT_CONFIRMED = _Resume(resumed=False, exhausted=False, data={})


def _exit_confirmed(row: aiosqlite.Row) -> bool:
    """`agent_ended` is a confirmed exit only when the host's observations of this
    attempt, after its claim, saw the session gone without a break for the
    channel's grace, up to its last one (3.2, 3.7): one `gone` listing between a
    restart and a crash is not a confirmation. The last observation is not held to
    the staleness window: the agent ended because it went silent."""
    observation, at, gone_at, claimed_at = (
        row["observation"],
        row["observation_at"],
        row["session_gone_at"],
        row["claimed_at"],
    )
    if observation is None or at is None or gone_at is None or claimed_at is None:
        return False
    last, since = datetime.fromisoformat(at), datetime.fromisoformat(gone_at)
    if since < datetime.fromisoformat(claimed_at):
        return False
    if (last - since).total_seconds() < SESSION_GONE_STABLE_S:
        return False
    try:
        return LeaseObservation.model_validate_json(observation).session == "gone"
    except ValidationError:
        return False


async def _auto_resume(tx: Tx, row: aiosqlite.Row, *, retries: bool) -> _Resume:
    """Apply the automatic resume rule to a confirmed exit of the claimed attempt.

    Every read is one seek on `events_task_kind` (migration 0057); the epoch is
    the latest `task.retried` or non-`system` `task.unblock`."""
    task_id = row["id"]
    epoch = max(
        await _latest_event(tx, task_id, "task.retried"),
        await _latest_event(tx, task_id, "task.unblock", operator=True),
    )
    resumed_at = await _latest_event(tx, task_id, "task.auto_resumed", after=epoch)
    if not resumed_at or await _progressed(tx, row, after=resumed_at):
        return _Resume(resumed=retries, exhausted=False, data={})
    return _Resume(resumed=False, exhausted=True, data={"auto_resume": "no_progress"})


# The automatic resume rule's reads; each is one seek on `events_task_kind`
# (migration 0057), checked by EXPLAIN QUERY PLAN in test_auto_resume.py.
LATEST_TASK_EVENT_SQL = (
    "SELECT id FROM events WHERE subject_type = 'task' AND subject_id = ?"
    " AND kind = ? AND id > ? AND (? = 0 OR actor <> 'system')"
    " ORDER BY id DESC LIMIT 1"
)
ATTEMPT_PROGRESS_SQL = (
    "SELECT 1 FROM events WHERE subject_type = 'task' AND subject_id = ?"
    " AND kind = 'task.progress' AND id > ?"
    " AND json_extract(data, '$.attempt') = ? LIMIT 1"
)


async def _latest_event(
    tx: Tx, task_id: str, kind: str, *, after: int = 0, operator: bool = False
) -> int:
    """Id of the task's newest `kind` event after `after`, or 0. `operator` skips
    the ones the server recorded on its own (actor `system`)."""
    async with tx.conn.execute(
        LATEST_TASK_EVENT_SQL, (task_id, kind, after, int(operator))
    ) as cur:
        found = await cur.fetchone()
    return found[0] if found else 0


async def _progressed(tx: Tx, row: aiosqlite.Row, *, after: int) -> bool:
    """Whether the claimed attempt recorded a `task.progress` (a milestone is one)
    after event `after`. Attempt numbers only grow within an epoch and a late
    write of an earlier attempt is refused, so the attempt alone places it."""
    async with tx.conn.execute(
        ATTEMPT_PROGRESS_SQL, (row["id"], after, row["attempt"])
    ) as cur:
        return await cur.fetchone() is not None


async def _record_auto_resumed(
    tx: Tx, row: aiosqlite.Row, actor: str, reason: str
) -> None:
    await record(
        tx,
        actor,
        "task",
        row["id"],
        "task.auto_resumed",
        {"attempt": row["attempt"], "reason": reason},
    )


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
    review: Review | None = None,
    open_review_decision: bool = True,
    exhausted: bool = False,
) -> TaskState:
    """Validate and persist one transition, its event (`task.<action>` unless
    `event` names it), and its knock-on effects.

    `counts_failure=False` evaluates a fail/expire as if no failure had counted, so it
    can never exhaust the attempts (the caller leaves `failures` unchanged).
    `open_review_decision=False` enters `in_review` without a human/orchestrator
    decision (independent code review gate; settings.spec.md 3.5).
    `exhausted=True` evaluates a fail/expire as if no attempt were left (the
    automatic resume rule's fail with the cause, failure classes 3.2)."""
    old = S(row["state"])
    failures = row["failures"] if counts_failure else -1
    ctx = TransitionContext(
        failures=row["max_attempts"] if exhausted else failures,
        max_attempts=row["max_attempts"],
        review=review or Review(row["review"]),
        deps_done=deps_done,
    )
    new = transition(old, action, ctx)
    # strictly greater than the old version, so a transition always stales a pending edit
    bumped = ts(max(tx.now, datetime.fromisoformat(row["updated_at"]) + _TICK))
    fields: dict[str, Any] = {
        "state": new,
        "updated_at": bumped,
        "retry_at": None,  # only a transient fail's requeue waits (failure classes 3.2)
        **(set_ or {}),
    }
    leaving_claim = old is S.CLAIMED and new is not S.CLAIMED
    if leaving_claim:
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
    if leaving_claim:
        # Pending steers belong to this attempt only (task steering spec 3.3).
        await steers.expire_attempt(tx, row["id"], row["attempt"])
        await _ensure_usage_outcome(tx, row, action, data or {})
    # Decisions that waited on the old state are moot once the task leaves it
    # (an answered decision is already closed before its transition runs).
    if new in TERMINAL:
        await decisions.cancel_open(tx, "task", row["id"])
    elif old in decisions.WAITS_IN and new is not old:
        await decisions.cancel_open(tx, "task", row["id"], decisions.WAITS_IN[old])
    if old is S.CLAIMED and action in _INTERRUPTIONS:
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


def _interruption_reason(action: Action, data: dict[str, Any]) -> str:
    fixed = _INTERRUPTIONS[action]
    reason = str(data.get("reason") or action)
    if fixed:
        return fixed
    return "asked" if reason.startswith("waiting for decision") else reason


# Actions that end a claim without a result: what the next attempt must be told.
# None: the reason is in the transition's data.
_INTERRUPTIONS: dict[Action, str | None] = {
    Action.FAIL: None,
    Action.EXPIRE: None,
    Action.RELEASE: "released",
    Action.REQUEUE: "quota",
    Action.BLOCK: None,
}

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


async def _record_result_usage(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    result: TaskResult,
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
        attempt=attempt,
        **usage.model_dump(exclude={"account_id"}),
    )


async def _check_artifacts(
    conn: aiosqlite.Connection, task_id: str, artifact_ids: list[str]
) -> None:
    if not artifact_ids:
        return
    marks = ",".join("?" * len(artifact_ids))
    async with conn.execute(
        f"SELECT id FROM artifacts WHERE source = 'worker' AND task_id = ?"  # noqa: S608
        f" AND id IN ({marks})",
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
    for f in _PIN_FIELDS:
        if data[f] is not None:
            data[f] = json.loads(data[f])
    data["requires_approval"] = bool(data["requires_approval"])
    data["continuation"] = effective(cont.load(row), data["acceptance"])
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
