"""Tokens and estimated cost per account, project and day (cost view spec).

Read only. The database sums by group; Python only prices the grouped rows.
"""

from collections.abc import Callable, Iterable
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
from argus_api.accounts.pricing import PriceTable
from argus_api.db import ts

WINDOWS: dict[CostWindow, timedelta] = {
    "24h": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
NO_MODEL = "(no model)"

# Per (group, model): runs, measured, not_reported, token sums and reported cost
# over measured rows (SUM of only NULLs is NULL, which keeps "no figure" apart
# from zero), and the measured runs that carry any token figure (the priceable ones).
_FIGURES = (
    "u.model, count(*), coalesce(sum(u.measurement = 'measured'), 0),"
    " coalesce(sum(u.measurement = 'not_reported'), 0),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.input_tokens END),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.output_tokens END),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.cached_input_tokens END),"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.cost_usd END),"
    " coalesce(sum(u.measurement = 'measured' AND (u.input_tokens IS NOT NULL"
    "  OR u.output_tokens IS NOT NULL OR u.cached_input_tokens IS NOT NULL)), 0)"
)
_WINDOW = " FROM account_usage u LEFT JOIN tasks t ON t.id = u.task_id WHERE u.at > ? AND u.at <= ?"
# Every real UTC offset is a multiple of 15 minutes, so a 15-minute slot always
# falls inside one local day.
_SLOT = (
    "substr(u.at, 1, 14) || printf('%02d', CAST(substr(u.at, 15, 2) AS INTEGER)"
    " / 15 * 15)"
)


class _Acc:
    """Running figures of one group; folds in one (group, model) row at a time."""

    def __init__(self) -> None:
        self.runs = self.measured = self.not_reported = self.unpriced = 0
        self.unpriced_models: set[str] = set()
        self.tokens: list[int | None] = [None, None, None]
        self.estimated: float | None = None
        self.reported: float | None = None

    def add(self, table: PriceTable, row: Iterable) -> None:
        model, n, measured, nr, i, o, c, reported, priceable = row
        self.runs += n
        self.measured += measured
        self.not_reported += nr
        for k, v in enumerate((i, o, c)):
            if v is not None:
                self.tokens[k] = (self.tokens[k] or 0) + v
        if reported is not None:
            self.reported = (self.reported or 0.0) + reported
        if not priceable:
            return
        cost = table.cost(model, i, o, c)
        if cost is None:
            self.unpriced += priceable
            self.unpriced_models.add(model or NO_MODEL)
        else:
            self.estimated = (self.estimated or 0.0) + cost

    def merge(self, other: "_Acc") -> None:
        self.runs += other.runs
        self.measured += other.measured
        self.not_reported += other.not_reported
        self.unpriced += other.unpriced
        self.unpriced_models |= other.unpriced_models
        for k, v in enumerate(other.tokens):
            if v is not None:
                self.tokens[k] = (self.tokens[k] or 0) + v
        if other.estimated is not None:
            self.estimated = (self.estimated or 0.0) + other.estimated
        if other.reported is not None:
            self.reported = (self.reported or 0.0) + other.reported

    def figures(self) -> CostFigures:
        return CostFigures(
            runs=self.runs,
            measured_runs=self.measured,
            not_reported_runs=self.not_reported,
            input_tokens=self.tokens[0],
            output_tokens=self.tokens[1],
            cached_input_tokens=self.tokens[2],
            estimated_cost_usd=self.estimated,
            reported_cost_usd=self.reported,
            unpriced_runs=self.unpriced,
        )


async def _group[K](
    conn: aiosqlite.Connection,
    key_sql: str,
    start: datetime,
    end: datetime,
    table: PriceTable,
    bucket: Callable[[str | None], K],
) -> dict[K, _Acc]:
    groups: dict[K, _Acc] = {}
    async with conn.execute(
        f"SELECT {key_sql}, {_FIGURES}{_WINDOW} GROUP BY 1, u.model",
        (ts(start), ts(end)),
    ) as cur:
        async for key, *rest in cur:
            groups.setdefault(bucket(key), _Acc()).add(table, rest)
    return groups


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
    by_acc = await _group(conn, "u.account_id", start, now, table, ident)
    by_proj = await _group(conn, "t.project", start, now, table, ident)
    per_day = await _group(
        conn,
        _SLOT,
        start,
        now,
        table,
        lambda slot: usage.floor(datetime.fromisoformat(f"{slot}+00:00"), "day", zone),
    )

    # Totals are the sum of the listed accounts, so they match the table.
    totals = _Acc()
    by_account = []
    for a in accounts:
        acc = by_acc.get(a.id) or _Acc()
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
    daily = [
        DayCost(start=d, **per_day.get(d, _Acc()).figures().model_dump())
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
            unpriced_models=sorted(totals.unpriced_models),
        ),
        totals=totals.figures(),
        by_account=by_account,
        by_project=by_project,
        daily=daily,
    )
