"""Claiming: handing a queued task and its lease to a worker."""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS
from argus_core.agents import AgentState
from argus_core.tasks import Action, Task

from argus_api.access import record_lease_grant
from argus_api.db import Tx, ts
from argus_api.disk_guard import DiskGuard
from argus_api.errors import ConflictError
from argus_api.events import record
from argus_api.runtime.gate import claim_gate
from argus_api.tasks import cost as task_cost
from argus_api.tasks.cost import BUDGET_EXHAUSTED
from argus_api.tasks.edit import _snapshot_attempt
from argus_api.tasks.engine import (
    _check_engine_lease,
    _record_runtime_providers,
    _restriction_met,
    awaiting_quota,
    can_serve,
    engine_choice_for,
    manages_accounts,
    wake_waiting,
)
from argus_api.tasks.reads import _TICK, _agent_state, _deadline, get_task
from argus_api.tasks.reasons import AWAITING_QUOTA, NO_COMPATIBLE_WORKER, RUNTIME_FAILED
from argus_api.tasks.transitions import _apply, _budget_block


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
