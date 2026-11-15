"""Task operations. Every write takes an open `Tx` and records its events in it.

See docs/specs/worker-lifecycle.spec.md 3.2 and 3.3.
"""

import contextlib
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS
from argus_core.agents import AgentState
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ChecklistItem,
    Continuation,
    ContinuationArtifact,
    ContinuationProvenance,
    ContinuationUpdate,
    ContinuationWip,
    EffectiveContinuation,
    EnvCheck,
    ItemStatus,
    ObservationRequest,
    clean,
    merge_acceptance,
)
from argus_core.decisions import Audience, DecisionKind
from argus_core.difficulty import DifficultySource, classify
from argus_core.kinds import TaskKind
from argus_core.tasks import (
    ENV_OOM,
    MAX_DEPENDENCIES,
    Action,
    Deliverable,
    LeaseObservation,
    PromptTrust,
    RetryClass,
    Review,
    StallCause,
    Task,
    TaskCreate,
    TaskResult,
    retry_class,
    retry_delay_s,
    validate_milestone,
)

from argus_api import orchestrator
from argus_api.access import record_lease_grant
from argus_api.accounts.usage import insert_usage
from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.disk_guard import DiskGuard
from argus_api.errors import (
    ConflictError,
    ForbiddenError,
    LeaseConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.integrations import selection
from argus_api.kinds import service as kinds
from argus_api.runtime.gate import claim_gate
from argus_api.tasks import continuation as cont
from argus_api.tasks import cost as task_cost
from argus_api.tasks import defaults as task_defaults
from argus_api.tasks import deliverable, steers
from argus_api.tasks.archive import archive, archive_matching, unarchive
from argus_api.tasks.cost import BUDGET_EXHAUSTED
from argus_api.tasks.edit import (
    _check_new_pins,
    _check_pages_exist,
    _digest,
    _pin_json,
    _providers_json,
    _resolve_harness_account,
    _snapshot_attempt,
    attempt_definitions,
    attempt_summaries,
    edit_task,
    set_dependencies,
)
from argus_api.tasks.engine import (
    _check_engine_lease,
    _record_runtime_providers,
    _restriction_met,
    attempt_model_switch,
    awaiting_quota,
    can_serve,
    engine_capacity,
    engine_choice_for,
    executor_provider,
    manages_accounts,
    prefers_local,
    record_model_switch,
    wake_waiting,
)
from argus_api.tasks.exits import (
    _NOT_CONFIRMED,
    ATTEMPT_PROGRESS_SQL,
    AUTO_RESUME_REASONS,
    EXIT_ERROR_MAX,
    LATEST_TASK_EVENT_SQL,
    OOM_PROMOTE_STREAK,
    _auto_resume,
    _exit_confirmed,
    _oom_outcome,
    _record_auto_resumed,
)
from argus_api.tasks.leases import (
    Lease,
    RejectedLease,
    RenewedLease,
    _owned,
    check_lease,
    clear_paused_disk,
    mark_paused_disk,
    renew,
    renew_paused_disk,
)
from argus_api.tasks.manage import (
    ENV_DISK_FULL,
    RELEASE_REASON_MAX,
    approve,
    cancel,
    check_reviewer,
    release,
    release_agent_tasks,
    retry,
    review,
    review_failed,
    shed_disk_full,
    unblock,
)
from argus_api.tasks.reads import (
    _TICK,
    S,
    _agent_state,
    _deadline,
    _row,
    _states,
    get_task,
    task_row,
)
from argus_api.tasks.reasons import (
    ADMISSION_REASONS,
    AWAITING_QUOTA,
    NO_COMPATIBLE_WORKER,
    RUNTIME_FAILED,
    RUNTIME_PREPARING,
    RUNTIME_SWITCH_FAILED,
    WORKER_GAVE_UP,
)
from argus_api.tasks.transitions import (
    ESCALATING_REASONS,
    _apply,
    _budget_block,
    _dependency_failed_event,
    _escalation,
    check_acceptance_met,
    moot_decisions,
    planned_state,
    review_escalation,
    unmet_acceptance,
)

__all__ = [
    "ATTEMPT_PROGRESS_SQL",
    "ENV_DISK_FULL",
    "EXIT_ERROR_MAX",
    "LATEST_TASK_EVENT_SQL",
    "LATE_DETECT_AFTER_S",
    "MAX_DEPENDENCIES",
    "OBSERVATION_STALE_S",
    "QUIET_CLAIM_S",
    "RELEASE_REASON_MAX",
    "RUNTIME_FAILED",
    "RUNTIME_PREPARING",
    "RUNTIME_SWITCH_FAILED",
    "Lease",
    "LeaseConflictError",
    "RejectedLease",
    "RenewedLease",
    "TaskResult",
    "ValidationFailedError",
    "_apply",
    "_owned",
    "_row",
    "add_clarification",
    "answered_equivalent",
    "approve",
    "archive",
    "archive_matching",
    "attempt_definitions",
    "attempt_model_switch",
    "attempt_summaries",
    "await_quota_ready",
    "awaiting_quota",
    "block",
    "block_switch_failed",
    "cancel",
    "check_acceptance_met",
    "check_create",
    "check_lease",
    "check_reviewer",
    "claim",
    "clear_paused_disk",
    "complete",
    "continuation_of",
    "create_task",
    "edit_task",
    "engine_capacity",
    "engine_choice_for",
    "executor_provider",
    "expire_agent_leases",
    "expire_leases",
    "expire_stalled",
    "expire_timeouts",
    "fail",
    "fail_runtime_holds",
    "flag_quiet",
    "get_task",
    "hold_for_runtime",
    "manages_accounts",
    "mark_paused_disk",
    "moot_decisions",
    "observe",
    "pause_for_quota",
    "planned_state",
    "prefers_local",
    "quiet_facts",
    "quiet_facts_for",
    "record_model_switch",
    "release",
    "release_agent_tasks",
    "release_runtime_holds",
    "renew",
    "renew_paused_disk",
    "report_progress",
    "retry",
    "review",
    "review_escalation",
    "review_failed",
    "set_dependencies",
    "shed_disk_full",
    "stall_cause",
    "task_row",
    "unarchive",
    "unblock",
    "unmet_acceptance",
    "update_continuation",
    "wake_waiting",
]

log = logging.getLogger(__name__)


# -- creation ----------------------------------------------------------------


async def create_task(
    tx: Tx,
    body: TaskCreate,
    actor: str,
    *,
    as_worker: str | None = None,
    hourly_limit: int | None = None,
    untrusted: bool = False,
    idempotency_key: str | None = None,
    chat_turn: str | None = None,
) -> Task:
    """Create a task.

    `as_worker` (an agent id) limits creation to subtasks of a task that agent
    currently holds (3.6). `hourly_limit` is the policy gate (3.10): past that
    many tasks by `actor` in the last hour, the task waits for human approval
    instead of being refused. `untrusted` gives the task `worker` prompt trust
    without forcing approval (routine findings, routines spec 3.3). With an
    `idempotency_key`, a repeat of the same request by the same actor returns
    the task it created; another request under that key is a conflict.
    """
    if idempotency_key is None:
        return await _create_task(
            tx,
            body,
            actor,
            as_worker,
            hourly_limit=hourly_limit,
            untrusted=untrusted,
            chat_turn=chat_turn,
        )
    # What was sent, not what defaults would fill: an omitted and a null budget
    # differ, and a replay after a settings change returns the same task (3.3).
    digest = hashlib.sha256(
        body.model_dump_json(exclude_unset=True).encode()
    ).hexdigest()
    async with tx.conn.execute(
        "SELECT request_digest, task_id FROM task_creation_keys"
        " WHERE actor = ? AND idempotency_key = ?",
        (actor, idempotency_key),
    ) as cur:
        seen = await cur.fetchone()
    if seen is not None:
        if seen["request_digest"] != digest:
            raise ConflictError(
                "this Idempotency-Key was used for another task creation request",
                code="idempotency_key_reused",
            )
        return await get_task(tx.conn, seen["task_id"])
    task = await _create_task(
        tx,
        body,
        actor,
        as_worker,
        hourly_limit=hourly_limit,
        untrusted=untrusted,
        chat_turn=chat_turn,
    )
    await tx.conn.execute(
        "INSERT INTO task_creation_keys (actor, idempotency_key, request_digest,"
        " task_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (actor, idempotency_key, digest, task.id, ts(tx.now)),
    )
    return task


async def _create_task(
    tx: Tx,
    body: TaskCreate,
    actor: str,
    as_worker: str | None,
    *,
    hourly_limit: int | None,
    untrusted: bool,
    chat_turn: str | None = None,
) -> Task:
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
    own_pins = body.knowledge
    kind = await kinds.get_kind(tx.conn, body.kind)
    body = _apply_kind_defaults(body, kind)
    if body.project is None:
        if body.parent_id is None:
            raise ValidationFailedError("project is required", code="project_required")
        body = body.model_copy(update={"project": parent["project"]})
    else:  # only an explicit name is refused: legacy `global` parents still inherit
        _refuse_global(body.project)
    # The project first, then its task defaults (task defaults spec 3.2).
    body, default_sources = task_defaults.resolve(
        body, kind, await task_defaults.stored(tx.conn, body.project)
    )
    # Worker-authored child tasks: immutable worker trust; always await approval so
    # injected prompts cannot silently become ready work for another agent.
    if as_worker is not None:
        body = body.model_copy(update={"requires_approval": True})
        prompt_trust = PromptTrust.WORKER
    elif untrusted:
        prompt_trust = PromptTrust.WORKER
    else:
        prompt_trust = PromptTrust.ORCHESTRATOR
    await _check_project_and_pins(
        tx.conn, body, own_pins, task_defaults.hints(body.project, default_sources)
    )
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
        await orchestrator.task_origin(
            tx.conn, tx.now, actor.removeprefix("agent:"), chat_turn
        )
        if actor.startswith("agent:")
        else None
    )

    await tx.conn.execute(
        "INSERT INTO tasks (id, parent_id, kind, title, spec, acceptance, input,"
        " required_capabilities, priority, state, requires_approval, review,"
        " prompt_trust, lease_ttl_s, max_attempts, project, timeout_s, created_by,"
        " created_at, updated_at, difficulty, difficulty_source, account_providers,"
        " harness_model, executor_model, harness_account, origin_conversation_id,"
        " deliverable, budget_usd)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            body.parent_id,
            body.kind,
            body.title,
            body.spec,
            json.dumps(body.acceptance),
            json.dumps(body.input),
            json.dumps(sorted(set(body.required_capabilities or []))),
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
            body.deliverable,
            body.budget_usd,
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
    if body.integrations:
        assert body.project is not None  # resolved above
        await selection.select(tx, task_id, body.project, body.integrations)
    # The defaults applied, by source: the creation-time snapshot (3.3).
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.created",
        {"to": state, "defaults": default_sources},
    )
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
    disk_guard: DiskGuard | None = None,
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

    When `disk_guard` refuses admission, raises `ConflictError(code=disk_low)`
    before any attempt is spent or credential/engine lease is acquired.
    """
    if disk_guard is not None:
        disk_guard.require_admission()
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
    await _note_incompatible(tx, agent_id)
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
        # A spent budget starts no attempt: the task blocks instead (task cost 3.6).
        if (spent := await task_cost.exhausted_reason(tx.conn, candidate)) is not None:
            await _apply(
                tx,
                candidate,
                Action.BLOCK,
                f"agent:{agent_id}",
                set_=_budget_block(spent),
                data={"reason": BUDGET_EXHAUSTED, "error": spent},
            )
            continue
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
            "paused_disk_at": None,
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


async def _note_incompatible(tx: Tx, agent_id: str) -> None:
    """Say why a ready task nobody can run now is not being claimed: no live active
    worker declares its capabilities (a pool that could launch one does not count).
    Only the progress note and one `task.incompatible` event change; the task stays
    `ready`, no attempt or lease is spent, and the note is cleared when a compatible
    worker is live again (or the claim resets it)."""
    async with tx.conn.execute(
        "SELECT capabilities FROM agents WHERE state = ?", (AgentState.ACTIVE,)
    ) as cur:
        workers = [set(json.loads(r[0])) for r in await cur.fetchall()]
    offered = set().union(*workers)
    async with tx.conn.execute(
        "SELECT id, required_capabilities, progress_message, updated_at FROM tasks"
        " WHERE state = 'ready' AND (required_capabilities != '[]'"
        " OR progress_message LIKE ?)",
        (NO_COMPATIBLE_WORKER + "%",),
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        need = set(json.loads(r["required_capabilities"]))
        note: str | None = None
        if need and not any(need <= w for w in workers):
            missing = sorted(need - offered)
            note = NO_COMPATIBLE_WORKER + (
                f"no live worker declares {', '.join(missing)}"
                if missing
                else f"no single live worker declares all of {', '.join(sorted(need))}"
            )
        had = (r["progress_message"] or "").startswith(NO_COMPATIBLE_WORKER)
        if note == r["progress_message"] or (note is None and not had):
            continue
        await tx.conn.execute(
            "UPDATE tasks SET progress_message = ?, progress_percent = NULL,"
            " progress_at = ?, updated_at = ? WHERE id = ?",
            (
                note,
                ts(tx.now),
                ts(max(tx.now, datetime.fromisoformat(r["updated_at"]) + _TICK)),
                r["id"],
            ),
        )
        if note is not None:
            await record(
                tx,
                f"agent:{agent_id}",
                "task",
                r["id"],
                "task.incompatible",
                {"reason": note.removeprefix(NO_COMPATIBLE_WORKER)},
            )


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


async def complete(
    tx: Tx,
    agent_id: str,
    task_id: str,
    attempt: int,
    result: TaskResult,
    *,
    model_policy_path: Path | None = None,
    pr_search: deliverable.PrSearch | None = None,
    pr_gate: deliverable.ForgeGate | None = None,
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
    await _oom_outcome(tx, agent_id, oom_kill=False)
    await _check_artifacts(tx.conn, task_id, result.artifacts)
    have = cont.load(row)
    # The deliverable's evidence comes first: the worker still holds its claim and can
    # fix the gap in this attempt (task deliverable spec 3.4).
    have = await deliverable.bind_found(tx, row, have, pr_search, f"agent:{agent_id}")
    acceptance = await settings_svc.acceptance_enabled(tx.conn, row["project"])
    awaits_forge = await deliverable.check_complete(
        tx.conn, row, agent_id, result, have, pr_search, pr_gate, acceptance
    )
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
    if not acceptance:
        open_items = []  # nothing holds the result in review (settings 3.11)
    elif not items and (row["attempt"] > 1 or have.artifacts or have.wip):
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
        data=(
            ({"acceptance_open": open_items} if open_items else {})
            | ({"deliverable_pending": row["deliverable"]} if awaits_forge else {})
        )
        or None,
        disposition=("not_reviewed", None if acceptance else "acceptance_off"),
        # Open criteria, or a PR that must still turn green or merge, need a decision.
        review=Review.NONE
        if not acceptance
        else Review.ORCHESTRATOR
        if (open_items or awaits_forge) and Review(row["review"]) is Review.NONE
        else None,
    )
    return await get_task(tx.conn, task_id)


PROGRESS_MIN_INTERVAL_S = 10


def readable_progress(message: str, kind: str | None = None) -> str:
    """One readable line for a progress message; a JSON object message is rendered."""
    text = message.strip()
    if not text.startswith("{"):
        return message
    try:
        payload = json.loads(text)
    except ValueError:
        return message
    if not isinstance(payload, dict):
        return message
    kind = str(payload.pop("kind", None) or kind or "update")
    if kind == "pr_opened" and "number" in payload and "url" in payload:
        line = f"PR opened: #{payload['number']} {payload['url']}"
    else:
        fields = ", ".join(
            f"{k} {v}"
            for k, v in payload.items()
            if isinstance(v, str | int | float) and not isinstance(v, bool)
        )
        line = f"{kind}: {fields}" if fields else kind
    line = " ".join(line.split())
    return line if len(line) <= 300 else line[:299] + "…"


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
    message = readable_progress(message, kind)
    row = await _owned(tx, task_id, attempt, agent_id)
    percent = percent if percent is not None else row["progress_percent"]
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
    *,
    oom_kill: bool = False,
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
    agents that cannot lease an account (account selection 3.6).

    `oom_kill` is the channel's evidence that the `session_lost` session was
    OOM-killed; the pool's streak of them may block the task as `env_oom`
    (failure classes 3.2.1)."""
    if oom_kill and reason != "session_lost":
        raise ValidationFailedError("oom_kill is only valid with reason session_lost")
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
    oom = None if quota else await _oom_outcome(tx, agent_id, oom_kill=oom_kill)
    if reason == "session_lost":
        # The channel saw the container gone for 90 s: a confirmed exit.
        error = _cap_utf8(error, EXIT_ERROR_MAX)
        fact: dict[str, Any] = {}
        if oom_kill:
            fact = {"oom_kill": True}
            if oom:
                fact |= {"pool": oom[0], "oom_streak": oom[1]}
        if oom_kill and oom and oom[1] >= OOM_PROMOTE_STREAK and retries:
            # The pool's sessions keep running out of memory: another attempt
            # there would too, so a human decides (failure classes 3.2.1).
            await _apply(
                tx,
                row,
                Action.BLOCK,
                f"agent:{agent_id}",
                set_={
                    "error": error,
                    "failures": failures,
                    "block_reason": ENV_OOM,
                    "oom_kill": 1,
                },
                data={
                    "error": error,
                    "reason": ENV_OOM,
                    "cause": reason,
                    "retry_class": RetryClass.ENVIRONMENT,
                }
                | fact,
            )
            return await get_task(tx.conn, task_id)
        resume = await _auto_resume(tx, row, retries=retries)
        await _apply(
            tx,
            row,
            Action.FAIL,
            f"agent:{agent_id}",
            set_={"error": error, "failures": failures, "oom_kill": int(oom_kill)}
            | harder.get("set", {}),
            data={"error": error, "reason": reason, "retry_class": kind}
            | fact
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
    *,
    void_claim: bool = False,
) -> Task:
    """The project's image is not usable for this claim: block the task before any
    model work, `attempt` and `failures` as they were. `void_claim` (the prepare
    step, before any executor started) also hands the claim's attempt number back,
    so the deferral spends nothing and the task starts with one attempt once the
    image is ready. `runtime_preparing` is woken
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
        voided=void_claim,
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


MERGE_STEER = (
    "The pull request for this task is merged. A merge is not acceptance: the "
    "checklist, evidence and review requirements still apply. Re-check them and "
    "finish the task."
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
    stored = await cont.store(
        tx, task_id, have, want, f"agent:{agent_id}", "observation"
    )
    if _newly_merged(have, stored):
        await _announce_merge(tx, row, f"agent:{agent_id}")
    return await get_task(tx.conn, task_id)


def _newly_merged(have: Continuation, now: Continuation) -> bool:
    """A pull request the record held as anything but merged is merged now."""
    before = {a.key: a.state for a in have.artifacts}
    return any(
        a.kind is ArtifactKind.PR
        and a.state is ArtifactState.MERGED
        and before.get(a.key) is not ArtifactState.MERGED
        for a in now.artifacts
    )


async def _announce_merge(tx: Tx, row: aiosqlite.Row, actor: str) -> None:
    """The first time a claimed task's PR is recorded merged, tell its holder (a steer
    on the attempt it holds) and the origin conversation. Once per merge: a repeat
    observation records no change. Merged is not acceptance; nothing else moves."""
    # The holder takes no steers, or one just went out: the record stands.
    with contextlib.suppress(ConflictError, RateLimitedError):
        await steers.create(tx, row["id"], MERGE_STEER, actor)
    if row["origin_conversation_id"]:
        # Imported here: conversations import the orchestrator, which imports us.
        from argus_api.conversations import service as conversations

        await conversations.queue_task_update(tx, row["origin_conversation_id"])


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
    0 disables; the attempt must already be flagged quiet, so the inbox warned first.
    A claim the disk guard paused is skipped: its silence is the guard's doing."""
    if stall_after_s <= 0:
        return []
    cutoff = tx.now - timedelta(seconds=stall_after_s)
    return await _expire_where(
        tx,
        "quiet_at IS NOT NULL AND paused_disk_at IS NULL AND ? IS NOT NULL",
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
    paused_disk_at: str | None = None,
) -> tuple[StallCause, str | None]:
    """Why a claim is silent, from the host's newest observation (3.7), with the
    observation time. Absent, stale (older than three heartbeats) or from before
    this attempt's claim: `unknown`. A claim the disk guard paused is `paused_disk`
    whatever the host saw: the API knows, it paused it (operations.spec.md 3.5).
    Descriptive only: no decision reads it."""
    if paused_disk_at is not None:
        return "paused_disk", None
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
        tx.now,
        row["observation"],
        row["observation_at"],
        row["claimed_at"],
        row["paused_disk_at"],
    )


_QUIET_SIGNALS = (
    "SELECT t.id, t.quiet_at, t.activity_at, t.progress_at, t.output_at, a.claimed_at,"
    " t.observation, t.observation_at, t.paused_disk_at, t.state"
    " FROM tasks t"
    " LEFT JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
)


class QuietFacts(NamedTuple):
    quiet_at: datetime | None
    last_signal_at: datetime | None
    last_signal_source: str | None
    stall_cause: StallCause | None


def quiet_facts_from_row(row: aiosqlite.Row | None, now: datetime) -> QuietFacts:
    """Quiet facts exist only while the task is claimed. A cause is given for a
    quiet claim, or for one the disk guard paused (`paused_disk`)."""
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
    paused_disk_at = row["paused_disk_at"] if claimed else None
    stall: StallCause | None = None
    if quiet_at is not None or paused_disk_at is not None:
        stall = stall_cause(
            now,
            row["observation"],
            row["observation_at"],
            row["claimed_at"],
            paused_disk_at,
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

    Sets `quiet_at` once per silence and emits `task.quiet`. Never expires. A claim
    the disk guard paused is skipped (operations.spec.md 3.5)."""
    async with tx.conn.execute(
        "SELECT t.*, a.claimed_at FROM tasks t"
        " JOIN task_attempts a ON a.task_id = t.id AND a.attempt = t.attempt"
        " WHERE t.state = 'claimed' AND t.quiet_at IS NULL"
        " AND t.paused_disk_at IS NULL ORDER BY t.id"
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
                    "UPDATE tasks SET quiet_at = ? WHERE id = ? AND quiet_at IS NULL"
                    " AND paused_disk_at IS NULL",
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
                await _oom_outcome(tx, row["assignee_agent_id"], oom_kill=False)
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


_APPROVAL_OPTIONS = ("approve", "reject")


async def _created_last_hour(tx: Tx, actor: str) -> int:
    async with tx.conn.execute(
        "SELECT count(*) FROM tasks WHERE created_by = ? AND created_at > ?",
        (actor, ts(tx.now - timedelta(hours=1))),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


def _apply_kind_defaults(body: TaskCreate, kind: TaskKind) -> TaskCreate:
    """Fill fields the creator left unset from the kind; validate the input. The
    capabilities, attempts and timeout rank below the task defaults
    (`task_defaults.resolve`)."""
    kinds.validate(kind.input_schema, body.input, "input")
    defaults = {
        "lease_ttl_s": kind.default_lease_ttl_s,
        "review": kind.default_review,
    }
    update: dict[str, Any] = {
        field: value
        for field, value in defaults.items()
        if value is not None and field not in body.model_fields_set
    }
    if kind.default_knowledge:  # pins merge rather than replace
        update["knowledge"] = list(
            dict.fromkeys([*kind.default_knowledge, *body.knowledge])
        )
    return body.model_copy(update=update)


async def check_create(conn: aiosqlite.Connection, body: TaskCreate) -> None:
    """The create rules that need no actor or parent: the kind and its input schema,
    the project and pinned pages. Routine templates are checked with it on save
    (routines spec 3.1)."""
    own_pins = body.knowledge
    kind = await kinds.get_kind(conn, body.kind)
    body = _apply_kind_defaults(body, kind)
    _refuse_global(body.project)
    body, sources = task_defaults.resolve(
        body, kind, await task_defaults.stored(conn, body.project)
    )
    await _check_project_and_pins(
        conn, body, own_pins, task_defaults.hints(body.project, sources)
    )


def _refuse_global(project: str | None) -> None:
    """`global` scopes knowledge and settings; a task needs a real project."""
    if project == "global":
        raise ValidationFailedError(
            "tasks need a real project; 'global' holds only knowledge and settings",
            code="project_required",
        )


async def _check_project_and_pins(
    conn: aiosqlite.Connection,
    body: TaskCreate,
    own_pins: list[str],
    hints: Callable[[deliverable.CreateProblem], list[str]],
) -> None:
    """`own_pins` are the caller's; a kind's pins are the admin's choice and may
    live in any project (task edit 3.1)."""
    if body.project is not None:
        async with conn.execute(
            "SELECT 1 FROM projects WHERE slug = ?", (body.project,)
        ) as cur:
            if await cur.fetchone() is None:
                raise NotFoundError(
                    f"unknown project: {body.project}", code="unknown_project"
                )
    await deliverable.check_creatable(
        conn,
        body.project,
        body.required_capabilities or [],
        body.deliverable or Deliverable.RESPONSE,
        hints,
    )
    if body.knowledge:
        await _check_pages_exist(conn, body.knowledge)
    if body.project is not None:
        await _check_new_pins(conn, body.project, list(dict.fromkeys(own_pins)))


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
    await task_cost.after_usage(tx, task_id)


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
