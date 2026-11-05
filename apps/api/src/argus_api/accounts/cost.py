"""Tokens and estimated cost per account, project and day (cost view spec).

Read only. The database sums by group; Python only prices the grouped rows.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from argus_core.accounts import REPOSITORY_PROVIDERS
from argus_core.cost import (
    AccountCost,
    CostFigures,
    CostReport,
    CostWindow,
    DayCost,
    PricingInfo,
    ProjectCost,
)

from argus_api.accounts import service, usage
from argus_api.accounts.figures import FIGURES, NO_MODEL, Acc
from argus_api.accounts.pricing import PriceTable
from argus_api.db import ts

WINDOWS: dict[CostWindow, timedelta] = {
    "24h": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

_WINDOW = " FROM account_usage u LEFT JOIN tasks t ON t.id = u.task_id WHERE u.at > ? AND u.at <= ?"
# Every real UTC offset is a multiple of 15 minutes, so a 15-minute slot always
# falls inside one local day.
_SLOT = (
    "substr(u.at, 1, 14) || printf('%02d', CAST(substr(u.at, 15, 2) AS INTEGER)"
    " / 15 * 15)"
)
# Distinct (task, attempt) coverage inside a group: measured when any row is.
_ATTEMPTS = (
    "SELECT grp, count(*) AS attempts,"
    " coalesce(sum(has_measured), 0) AS measured_attempts,"
    " coalesce(sum(NOT has_measured), 0) AS not_reported_attempts"
    " FROM ("
    "  SELECT {key} AS grp, u.task_id, u.attempt,"
    "   max(u.measurement = 'measured') AS has_measured"
    "  FROM account_usage u LEFT JOIN tasks t ON t.id = u.task_id"
    "  WHERE u.at > ? AND u.at <= ? AND u.attempt IS NOT NULL {extra}"
    "  GROUP BY 1, u.task_id, u.attempt"
    ") GROUP BY grp"
)


async def _group[K](
    conn: aiosqlite.Connection,
    key_sql: str,
    start: datetime,
    end: datetime,
    table: PriceTable,
    bucket: Callable[[str | None], K],
    *,
    extra: str = "",
) -> dict[K, Acc]:
    groups: dict[K, Acc] = {}
    async with conn.execute(
        f"SELECT {key_sql}, {FIGURES}{_WINDOW}{extra} GROUP BY 1, u.model",
        (ts(start), ts(end)),
    ) as cur:
        async for key, *rest in cur:
            groups.setdefault(bucket(key), Acc()).add(table, rest)
    await _attach_attempts(conn, key_sql, start, end, groups, bucket, extra=extra)
    return groups


async def _attach_attempts[K](
    conn: aiosqlite.Connection,
    key_sql: str,
    start: datetime,
    end: datetime,
    groups: dict[K, Acc],
    bucket: Callable[[str | None], K],
    *,
    extra: str = "",
) -> None:
    async with conn.execute(
        _ATTEMPTS.format(key=key_sql, extra=extra),
        (ts(start), ts(end)),
    ) as cur:
        async for key, attempts, measured, nr in cur:
            groups.setdefault(bucket(key), Acc()).set_attempts(
                int(attempts), int(measured), int(nr)
            )


def _order(name: str, f: CostFigures) -> tuple:
    """Measured groups first; priced before unpriced, by cost then tokens."""
    tokens = (f.input_tokens or 0) + (f.output_tokens or 0)
    return (
        f.measured_runs == 0,
        f.estimated_cost_usd is None,
        -(f.estimated_cost_usd or 0.0),
        -tokens,
        name,
    )


async def report(
    conn: aiosqlite.Connection,
    now: datetime,
    window: CostWindow,
    tz: str,
    zone: ZoneInfo,
    table: PriceTable,
) -> CostReport:
    start = now - WINDOWS[window]
    accounts = [
        a
        for a in await service.list_accounts(conn, now)
        if a.provider not in REPOSITORY_PROVIDERS
    ]
    ident = lambda k: k  # noqa: E731
    by_acc = await _group(
        conn,
        "u.account_id",
        start,
        now,
        table,
        ident,
        extra=" AND u.account_id IS NOT NULL",
    )
    unattributed = (
        await _group(
            conn,
            "NULL",
            start,
            now,
            table,
            lambda _k: None,
            extra=" AND u.account_id IS NULL",
        )
    ).get(None) or Acc()
    by_proj = await _group(conn, "t.project", start, now, table, ident)
    per_day = await _group(
        conn,
        _SLOT,
        start,
        now,
        table,
        lambda slot: usage.floor(datetime.fromisoformat(f"{slot}+00:00"), "day", zone),
    )

    # Totals are by_account plus unattributed, so the tables add up (cost view 3.3).
    totals = Acc()
    totals.merge(unattributed)
    by_account = []
    for a in accounts:
        acc = by_acc.get(a.id) or Acc()
        totals.merge(acc)
        by_account.append(
            AccountCost(
                account_id=a.id,
                name=a.name,
                provider=a.provider,
                quota=a.quota,
                quota_status=a.quota_status,
                **acc.figures().model_dump(),
            )
        )
    by_account.sort(key=lambda r: _order(r.name, r))
    by_project = [
        ProjectCost(project=p, **acc.figures().model_dump())
        for p, acc in by_proj.items()
    ]
    by_project.sort(key=lambda r: _order(r.project or "", r))
    orch = await _orchestrator_figures(conn, start, now)
    # Listed so the pricing banner names every unpriced model; the totals stay clean.
    pricing_unpriced = totals.unpriced_models | orch.unpriced_models
    daily = [
        DayCost(start=d, **per_day.get(d, Acc()).figures().model_dump())
        for d in usage.bucket_starts(start, now, "day", zone)
    ]
    return CostReport(
        window=window,
        start=start,
        end=now,
        tz=tz,
        pricing=PricingInfo(
            currency=table.currency,
            as_of=table.as_of,
            version=table.version,
            source=table.source,
            unpriced_models=sorted(pricing_unpriced),
        ),
        totals=totals.figures(),
        unattributed=unattributed.figures(),
        by_account=by_account,
        by_project=by_project,
        orchestrator=orch.figures(),
        daily=daily,
    )


async def _orchestrator_figures(
    conn: aiosqlite.Connection,
    start: datetime,
    end: datetime,
) -> Acc:
    """Sum conversation_usage for the window (internal-orchestrator 3.7).

    `cost_usd` was priced when the turn was recorded; we sum stored figures.
    """
    acc = Acc()
    async with conn.execute(
        "SELECT model, measurement, count(*),"
        " sum(input_tokens), sum(output_tokens), sum(cost_usd)"
        " FROM conversation_usage WHERE at > ? AND at <= ?"
        " GROUP BY model, measurement",
        (ts(start), ts(end)),
    ) as cur:
        async for model, measurement, n, i, o, cost in cur:
            n = int(n)
            acc.runs += n
            if measurement == "unknown":
                acc.not_reported += n
                continue
            acc.measured += n
            for k, v in enumerate((i, o)):
                if v is not None:
                    acc.tokens[k] = (acc.tokens[k] or 0) + int(v)
            if measurement == "unpriced":
                acc.unpriced += n
                acc.unpriced_models.add(model or NO_MODEL)
            elif cost is not None:
                acc.estimated = (acc.estimated or 0.0) + float(cost)
    return acc
