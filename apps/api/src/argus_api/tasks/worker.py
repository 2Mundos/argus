"""Worker reports: progress, completion, failure and blocking of a claimed task."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from argus_core.continuation import ContinuationProvenance, ItemStatus, merge_acceptance
from argus_core.quota import failure_class, sanitize_notice
from argus_core.tasks import (
    ENV_OOM,
    ENV_PROVIDER_AUTH,
    Action,
    RetryClass,
    Review,
    Task,
    TaskResult,
    retry_class,
    retry_delay_s,
    validate_milestone,
)

from argus_api.accounts.usage import insert_usage
from argus_api.db import Tx, ts
from argus_api.errors import RateLimitedError, ValidationFailedError
from argus_api.events import record
from argus_api.kinds import service as kinds
from argus_api.tasks import continuation as cont
from argus_api.tasks import cost as task_cost
from argus_api.tasks import deliverable
from argus_api.tasks.engine import can_serve, manages_accounts
from argus_api.tasks.exits import (
    EXIT_ERROR_MAX,
    OOM_PROMOTE_STREAK,
    _auto_resume,
    _oom_outcome,
    _record_auto_resumed,
)
from argus_api.tasks.leases import _owned
from argus_api.tasks.reads import _deadline, _row, get_task
from argus_api.tasks.reasons import ADMISSION_REASONS, AWAITING_QUOTA, WORKER_GAVE_UP
from argus_api.tasks.transitions import ESCALATING_REASONS, _apply, _escalation


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
    forge: deliverable.ForgeRead | None = None,
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

    # The stored artifacts (a PR bound at completion) are read back for the gate.
    row = await _row(tx.conn, task_id)
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
        forge=forge,
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
    if reason == "provider_failure" and failure_class(error) in (
        "provider_auth",
        "auth_expired",
    ):
        # The provider refused the credential before any work counted: the
        # environment is broken, not the task. Block it with the provider's own
        # words and spend no attempt (failure classes 3.2).
        note = sanitize_notice(error)
        await _apply(
            tx,
            row,
            Action.BLOCK,
            f"agent:{agent_id}",
            set_={
                "error": note,
                "block_reason": ENV_PROVIDER_AUTH,
                "progress_message": f"provider authentication failed: {note}",
            },
            data={
                "error": note,
                "reason": ENV_PROVIDER_AUTH,
                "cause": reason,
                "retry_class": RetryClass.ENVIRONMENT,
            },
        )
        return await get_task(tx.conn, task_id)
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


def _cap_utf8(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes of `text`, never splitting a character."""
    return text.encode()[:limit].decode(errors="ignore")


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
