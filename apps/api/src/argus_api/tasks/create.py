"""Task creation: validation, kind defaults and the creation write.

See docs/specs/task-defaults.spec.md.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.decisions import Audience, DecisionKind
from argus_core.difficulty import DifficultySource, classify
from argus_core.kinds import TaskKind
from argus_core.tasks import Deliverable, PromptTrust, Task, TaskCreate

from argus_api import orchestrator
from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.integrations import selection
from argus_api.kinds import service as kinds
from argus_api.tasks import defaults as task_defaults
from argus_api.tasks import deliverable
from argus_api.tasks.edit import (
    _check_new_pins,
    _check_pages_exist,
    _pin_json,
    _providers_json,
    _resolve_harness_account,
)
from argus_api.tasks.reads import S, _row, _states, get_task
from argus_api.tasks.transitions import _dependency_failed_event


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
