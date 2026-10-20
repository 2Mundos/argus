"""Reported usage rows, quota snapshots and their aggregation (account usage spec).

A leaf module: both the account and the task services write usage through it.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import aiosqlite
from argus_core import claude_auth, cursor_auth
from argus_core.accounts import (
    PERIOD_HOURS,
    AccountUsage,
    QuotaReport,
    QuotaSource,
    QuotaStatus,
    QuotaStatusWindow,
    QuotaWindow,
    Usage,
    UsageBucket,
)

from argus_api.db import Tx, ts
from argus_api.errors import ValidationFailedError
from argus_api.events import record

MAX_BUCKETS = 1000
Bucket = Literal["hour", "day"]


async def insert_usage(
    tx: Tx,
    account_id: str,
    agent_id: str | None,
    task_id: str | None,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    cost_usd: float | None = None,
    model: str | None = None,
    engine: str | None = None,
    role: str | None = None,
    run_key: str | None = None,
    measurement: str = "measured",
) -> None:
    """One row per report. A repeated `run_key` for the account adds nothing."""
    await tx.conn.execute(
        "INSERT INTO account_usage (account_id, agent_id, task_id, input_tokens,"
        " output_tokens, cached_input_tokens, cost_usd, model, engine, role, at,"
        " run_key, measurement) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT DO NOTHING",
        (
            account_id,
            agent_id,
            task_id,
            input_tokens,
            output_tokens,
            cached_input_tokens,
            cost_usd,
            model,
            engine,
            role,
            ts(tx.now),
            run_key,
            measurement,
        ),
    )


async def upsert_quota(
    tx: Tx,
    account_id: str,
    reports: list[QuotaReport],
    source: QuotaSource,
    actor: str,
) -> bool:
    """Replace each window's snapshot. Records one `account.quota` event when any
    figure changed (the observation time alone is not a change)."""
    before = {q.window: q for q in await list_quota(tx.conn, account_id)}
    changed = []
    for r in reports:
        resets = ts(r.resets_at) if r.resets_at else None
        until = ts(r.exhausted_until) if r.exhausted_until else None
        await tx.conn.execute(
            "INSERT INTO account_quota (account_id, window, window_minutes,"
            " used_percent, resets_at, exhausted_until, observed_at, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (account_id, window) DO"
            " UPDATE SET window_minutes = excluded.window_minutes,"
            " used_percent = excluded.used_percent, resets_at = excluded.resets_at,"
            " exhausted_until = excluded.exhausted_until,"
            " observed_at = excluded.observed_at, source = excluded.source",
            (
                account_id,
                r.window,
                r.window_minutes,
                r.used_percent,
                resets,
                until,
                ts(tx.now),
                source,
            ),
        )
        old = before.get(r.window)
        if old is None or (
            old.used_percent,
            old.resets_at,
            old.exhausted_until,
        ) != (r.used_percent, r.resets_at, r.exhausted_until):
            changed.append(r.window)
    if changed:
        await record(
            tx, actor, "account", account_id, "account.quota", {"windows": changed}
        )
    return bool(changed)


async def list_quota(conn: aiosqlite.Connection, account_id: str) -> list[QuotaWindow]:
    async with conn.execute(
        "SELECT * FROM account_quota WHERE account_id = ?"
        " ORDER BY window_minutes IS NULL, window_minutes, window",
        (account_id,),
    ) as cur:
        return [QuotaWindow.model_validate(dict(r)) for r in await cur.fetchall()]


async def last_observed(conn: aiosqlite.Connection, account_id: str) -> datetime | None:
    async with conn.execute(
        "SELECT max(observed_at) FROM account_quota WHERE account_id = ?",
        (account_id,),
    ) as cur:
        row = await cur.fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


# -- aggregation --------------------------------------------------------------


def bucket_starts(
    start: datetime, end: datetime, bucket: Bucket, tz: ZoneInfo
) -> list[datetime]:
    """Every bucket that overlaps [start, end), as local starts in `tz`."""
    starts: list[datetime] = []
    if bucket == "day":
        day = start.astimezone(tz).date()
        while (first := datetime(day.year, day.month, day.day, tzinfo=tz)) < end:
            starts.append(first)
            day += timedelta(days=1)
            if len(starts) > MAX_BUCKETS:
                break
        return starts
    # Hours: step through UTC in 15-minute slots (every real offset is a multiple
    # of 15 minutes) and keep each distinct local hour once, across DST changes.
    cursor = start.astimezone(UTC).replace(second=0, microsecond=0)
    cursor -= timedelta(minutes=cursor.minute % 15)
    while cursor < end and len(starts) <= MAX_BUCKETS:
        local = floor(cursor, "hour", tz)
        if not starts or starts[-1] != local:
            starts.append(local)
        cursor += timedelta(minutes=15)
    return starts


def floor(at: datetime, bucket: Bucket, tz: ZoneInfo) -> datetime:
    local = at.astimezone(tz)
    if bucket == "day":
        return datetime(local.year, local.month, local.day, tzinfo=tz)
    return local.replace(minute=0, second=0, microsecond=0)


async def series(
    conn: aiosqlite.Connection,
    start: datetime,
    end: datetime,
    bucket: Bucket,
    tz: ZoneInfo,
    account_ids: list[str],
) -> list[AccountUsage]:
    starts = bucket_starts(start, end, bucket, tz)
    if len(starts) > MAX_BUCKETS:
        raise ValidationFailedError(
            f"too many buckets: at most {MAX_BUCKETS}; use a larger bucket",
            code="too_many_buckets",
        )
    # Minute granularity in SQL keeps the rows few; the local bucket is
    # computed here, where the timezone rules are.
    sums: dict[str, dict[datetime, list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0, 0, 0.0, 0, 0])
    )
    async with conn.execute(
        "SELECT account_id, substr(at, 1, 16) AS minute,"
        " coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0),"
        " coalesce(sum(cached_input_tokens), 0), coalesce(sum(cost_usd), 0.0),"
        " count(*), coalesce(sum(measurement = 'not_reported'), 0)"
        " FROM account_usage WHERE at >= ? AND at < ?"
        " GROUP BY account_id, minute",
        (ts(start), ts(end)),
    ) as cur:
        for account_id, minute, i, o, c, cost, n, u in await cur.fetchall():
            at = datetime.fromisoformat(minute + ":00+00:00")
            acc = sums[account_id][floor(at, bucket, tz)]
            for k, v in enumerate((i, o, c, cost, n, u)):
                acc[k] += v
    async with conn.execute("SELECT DISTINCT account_id FROM account_usage") as cur:
        reported = {r[0] for r in await cur.fetchall()}
    out = []
    for account_id in account_ids:
        per = sums.get(account_id, {})
        buckets = [
            UsageBucket(
                start=s,
                input_tokens=int((v := per.get(s, [0, 0, 0, 0.0, 0, 0]))[0]),
                output_tokens=int(v[1]),
                cached_input_tokens=int(v[2]),
                cost_usd=float(v[3]),
                runs=int(v[4]),
                unmeasured_runs=int(v[5]),
            )
            for s in starts
        ]
        out.append(
            AccountUsage(
                account_id=account_id,
                reported=account_id in reported,
                totals=Usage(
                    input_tokens=sum(b.input_tokens for b in buckets),
                    output_tokens=sum(b.output_tokens for b in buckets),
                    cached_input_tokens=sum(b.cached_input_tokens for b in buckets),
                    cost_usd=sum(b.cost_usd for b in buckets),
                    runs=sum(b.runs for b in buckets),
                    unmeasured_runs=sum(b.unmeasured_runs for b in buckets),
                ),
                buckets=buckets,
            )
        )
    return out


# -- quota status ---------------------------------------------------------------

# Providers that expose no quota Argus may read, however the account is used.
_NO_QUOTA_PROVIDERS = frozenset({claude_auth.PROVIDER, cursor_auth.PROVIDER})


async def quota_status(
    conn: aiosqlite.Connection,
    account: dict,
    snapshots: list[QuotaWindow],
    now: datetime,
) -> QuotaStatus:
    """Do we know this account's quota? Provider snapshots first, then local
    estimates against `token_limits`; otherwise unknown with the reason."""
    if snapshots:
        return QuotaStatus(state="known")
    limits = json.loads(account["token_limits"]) if account["token_limits"] else {}
    if limits:
        windows = []
        for period, hours in PERIOD_HOURS.items():
            if period not in limits:
                continue
            async with conn.execute(
                "SELECT coalesce(sum(coalesce(input_tokens, 0)"
                " + coalesce(output_tokens, 0)), 0) FROM account_usage"
                " WHERE account_id = ? AND measurement = 'measured' AND at > ?",
                (account["id"], ts(now - timedelta(hours=hours))),
            ) as cur:
                row = await cur.fetchone()
                used = int(row[0]) if row else 0
            limit = int(limits[period])
            windows.append(
                QuotaStatusWindow(
                    period=period,  # ty: ignore[invalid-argument-type]
                    limit=limit,
                    used=used,
                    remaining=max(limit - used, 0),
                    used_percent=min(round(used / limit * 100, 1), 100.0),
                )
            )
        return QuotaStatus(state="known", windows=windows)
    async with conn.execute(
        "SELECT 1 FROM account_usage WHERE account_id = ? LIMIT 1", (account["id"],)
    ) as cur:
        if await cur.fetchone() is None:
            return QuotaStatus(state="unknown", reason="no_usage_records")
    if account["kind"] == "api_key" or account["provider"] in _NO_QUOTA_PROVIDERS:
        return QuotaStatus(state="unknown", reason="provider_does_not_expose")
    return QuotaStatus(state="unknown", reason="window_not_configured")
