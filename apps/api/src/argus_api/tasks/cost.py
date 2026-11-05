"""What a task has spent, and its optional budget (task cost spec).

The figures are the cost view's (accounts/figures.py) over the task's usage rows,
priced on read and inside the usage insert; nothing is stored but the budget.
"""

import logging
from typing import NamedTuple

import aiosqlite
from argus_core.cost import AttemptCost, BudgetState, PricingInfo, TaskCost

from argus_api.accounts import pricing
from argus_api.accounts.figures import FIGURES, NO_MODEL, Acc
from argus_api.accounts.pricing import PriceTable, PricingInvalidError
from argus_api.db import Tx
from argus_api.errors import ConflictError, NotFoundError, RateLimitedError
from argus_api.events import transient
from argus_api.tasks import steers

log = logging.getLogger(__name__)

BUDGET_ACTOR = "system:budget"  # accepted only from this module, never a token
BUDGET_EXHAUSTED = "budget_exhausted"
WARNING_AT = 0.8


class Spend(NamedTuple):
    totals: Acc
    by_attempt: dict[int, Acc]
    models: dict[int, set[str]]

    @property
    def partial(self) -> bool:
        return self.totals.unpriced > 0 or self.totals.not_reported > 0


async def spend(conn: aiosqlite.Connection, task_id: str, table: PriceTable) -> Spend:
    """One `GROUP BY attempt, model` over `account_usage_task` (3.1). Rows with no
    attempt count only in the totals."""
    totals = Acc()
    by_attempt: dict[int, Acc] = {}
    models: dict[int, set[str]] = {}
    async with conn.execute(
        f"SELECT u.attempt, {FIGURES} FROM account_usage u"  # noqa: S608 - constant
        " WHERE u.task_id = ? GROUP BY u.attempt, u.model",
        (task_id,),
    ) as cur:
        async for attempt, *rest in cur:
            acc = Acc()
            acc.add(table, rest)
            totals.merge(acc)
            if attempt is None:
                continue
            by_attempt.setdefault(attempt, Acc()).merge(acc)
            models.setdefault(attempt, set()).add(rest[0] or NO_MODEL)
    for acc in by_attempt.values():
        acc.set_attempts(1, int(acc.measured > 0), int(acc.measured == 0))
    totals.set_attempts(
        len(by_attempt),
        sum(a.measured_attempts for a in by_attempt.values()),
        sum(a.not_reported_attempts for a in by_attempt.values()),
    )
    return Spend(totals, by_attempt, models)


def budget_state(
    estimated: float | None, budget: float | None, partial: bool
) -> BudgetState:
    """3.2: on the estimate; exhausted on the known figure even when partial."""
    if budget is None:
        return "none"
    used = estimated or 0.0
    if used >= budget:
        return "exhausted"
    if used >= WARNING_AT * budget:
        return "partial_warning" if partial else "warning"
    return "partial_ok" if partial else "ok"


async def task_cost(
    conn: aiosqlite.Connection, task_id: str, table: PriceTable
) -> TaskCost:
    budget = await _budget(conn, task_id)
    s = await spend(conn, task_id, table)
    totals = s.totals.figures()
    return TaskCost(
        task_id=task_id,
        pricing=PricingInfo(
            currency=table.currency,
            as_of=table.as_of,
            version=table.version,
            source=table.source,
            unpriced_models=sorted(s.totals.unpriced_models),
        ),
        totals=totals,
        partial=s.partial,
        by_attempt=[
            AttemptCost(
                attempt=n,
                models=sorted(s.models[n]),
                **acc.figures().model_dump(),
            )
            for n, acc in sorted(s.by_attempt.items())
        ],
        budget_usd=budget,
        budget_state=budget_state(totals.estimated_cost_usd, budget, s.partial),
    )


async def exhausted_reason(
    conn: aiosqlite.Connection, row: aiosqlite.Row
) -> str | None:
    """The block message when the task's budget is spent (3.6), else None. With no
    usable price table nothing is known to be spent, so nothing is blocked."""
    budget = row["budget_usd"]
    if budget is None:
        return None
    try:
        table = pricing.in_force()
    except PricingInvalidError as exc:
        log.warning("budget check skipped for task %s: %s", row["id"], exc)
        return None
    s = await spend(conn, row["id"], table)
    estimated = s.totals.estimated
    if budget_state(estimated, budget, s.partial) != "exhausted":
        return None
    figure = "partial estimate" if s.partial else "estimate"
    known = " (unpriced or unreported runs not counted)" if s.partial else ""
    return (
        f"budget of ${budget:.2f} is spent ({figure} ${estimated or 0.0:.2f}{known});"
        " raise the budget or cancel the task"
    )


async def after_usage(tx: Tx, task_id: str | None) -> None:
    """In the usage-insert transaction (3.3, 3.5): publish `task.cost` and, the
    first time an attempt crosses the budget, steer it to wrap up."""
    if task_id is None:
        return
    async with tx.conn.execute(
        "SELECT id, state, attempt, budget_usd, budget_steered_attempt"
        " FROM tasks WHERE id = ?",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    try:
        table = pricing.in_force()
    except PricingInvalidError as exc:
        log.warning("task cost skipped for task %s: %s", task_id, exc)
        return
    s = await spend(tx.conn, task_id, table)
    budget = row["budget_usd"]
    state = budget_state(s.totals.estimated, budget, s.partial)
    transient(
        tx,
        "system",
        "task",
        task_id,
        "task.cost",
        {
            "task_id": task_id,
            "attempt": row["attempt"],
            "estimated_cost_usd": s.totals.estimated,
            "partial": s.partial,
            "budget_usd": budget,
            "budget_state": state,
        },
    )
    if (
        state != "exhausted"
        or row["state"] != "claimed"
        or row["budget_steered_attempt"] == row["attempt"]
    ):
        return
    message = (
        f"The task's budget of ${budget:.2f} is spent (estimate"
        f" ${s.totals.estimated or 0.0:.2f}). Stop starting new work: commit and push"
        " what you have, then finish with ASK: describing what is left."
    )
    try:
        async with tx.savepoint():
            await steers.create(tx, task_id, message, BUDGET_ACTOR)
    except (ConflictError, RateLimitedError) as exc:
        # No steer feature, or the steer limits: the attempt runs to its own end.
        log.info("budget steer not sent for task %s: %s", task_id, exc)
        if getattr(exc, "code", None) != "steer_unsupported":
            return
    await tx.conn.execute(
        "UPDATE tasks SET budget_steered_attempt = ? WHERE id = ?",
        (row["attempt"], task_id),
    )


async def _budget(conn: aiosqlite.Connection, task_id: str) -> float | None:
    async with conn.execute(
        "SELECT budget_usd FROM tasks WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"task not found: {task_id}")
    return row["budget_usd"]
