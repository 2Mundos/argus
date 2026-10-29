"""Reported usage rows, quota snapshots and their aggregation (account usage spec).

A leaf module: both the account and the task services write usage through it.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import aiosqlite
from argus_core import claude_auth, codex_auth, cursor_auth
from argus_core.accounts import (
    PERIOD_HOURS,
    PLACEHOLDER_PREFIX,
    AccountCredit,
    AccountUsage,
    QuotaCollector,
    QuotaProbe,
    QuotaReport,
    QuotaSource,
    QuotaStatus,
    QuotaStatusReason,
    QuotaStatusWindow,
    QuotaWindow,
    Usage,
    UsageBucket,
)
from argus_core.custom_accounts import OPENROUTER
from argus_core.custom_accounts import PROVIDER as CUSTOM_PROVIDER

from argus_api.db import Tx, ts
from argus_api.errors import ValidationFailedError
from argus_api.events import record

MAX_BUCKETS = 1000


@dataclass
class CreditReading:
    """A provider's credit figure for one key, in USD. `limit`/`remaining` are None
    for a key with no cap; `used` is the provider's own total, never inferred."""

    limit: float | None
    remaining: float | None
    used: float | None
    limit_reset: str | None = None
    free_tier: bool | None = None
    resets_at: datetime | None = None


Bucket = Literal["hour", "day"]


async def resolve_attempt(
    conn: aiosqlite.Connection,
    agent_id: str,
    task_id: str | None,
    attempt: int | None,
) -> int | None:
    """The attempt this report belongs to (account usage 3.10), or None."""
    if task_id is None:
        return None
    if attempt is not None:
        # An attempt the agent did not claim here is not attributed to another.
        async with conn.execute(
            "SELECT 1 FROM task_attempts WHERE task_id = ? AND attempt = ?"
            " AND agent_id = ?",
            (task_id, attempt, agent_id),
        ) as cur:
            return attempt if await cur.fetchone() else None
    async with conn.execute(
        "SELECT attempt FROM task_attempts WHERE task_id = ? AND agent_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, agent_id),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def insert_usage(
    tx: Tx,
    account_id: str | None,
    agent_id: str | None,
    task_id: str | None,
    *,
    attempt: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    cost_usd: float | None = None,
    model: str | None = None,
    engine: str | None = None,
    role: str | None = None,
    run_key: str | None = None,
    measurement: str = "measured",
    reason: str | None = None,
) -> None:
    """One row per report. A repeated `run_key` for the account adds nothing.

    A measured report for an attempt whose only row is an `a1:` placeholder upgrades
    that row in place (account usage 3.10). A not_reported report never downgrades
    a placeholder or a measured row.
    """
    unmeasured = reason if measurement == "not_reported" else None
    at = ts(tx.now)
    if (
        measurement == "measured"
        and task_id is not None
        and attempt is not None
        and await _upgrade_placeholder(
            tx,
            account_id=account_id,
            agent_id=agent_id,
            task_id=task_id,
            attempt=attempt,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cost_usd=cost_usd,
            model=model,
            engine=engine,
            role=role,
            run_key=run_key,
            at=at,
        )
    ):
        return
    if (
        measurement == "not_reported"
        and task_id is not None
        and attempt is not None
        and await _attempt_has_placeholder(tx.conn, task_id, attempt)
    ):
        return  # never add a second not_reported over the API's own placeholder
    await tx.conn.execute(
        "INSERT INTO account_usage (account_id, agent_id, task_id, attempt,"
        " input_tokens, output_tokens, cached_input_tokens, cost_usd, model, engine,"
        " role, at, run_key, measurement, unmeasured_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT DO NOTHING",
        (
            account_id,
            agent_id,
            task_id,
            attempt,
            input_tokens,
            output_tokens,
            cached_input_tokens,
            cost_usd,
            model,
            engine,
            role,
            at,
            run_key,
            measurement,
            unmeasured,
        ),
    )


async def _attempt_has_placeholder(
    conn: aiosqlite.Connection, task_id: str, attempt: int
) -> bool:
    async with conn.execute(
        "SELECT 1 FROM account_usage WHERE task_id = ? AND attempt = ?"
        " AND measurement = 'not_reported' AND substr(run_key, 1, 3) = ?"
        " LIMIT 1",
        (task_id, attempt, PLACEHOLDER_PREFIX),
    ) as cur:
        return await cur.fetchone() is not None


async def _upgrade_placeholder(
    tx: Tx,
    *,
    account_id: str | None,
    agent_id: str | None,
    task_id: str,
    attempt: int,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None,
    cost_usd: float | None,
    model: str | None,
    engine: str | None,
    role: str | None,
    run_key: str | None,
    at: str,
) -> bool:
    """Turn the attempt's sole `a1:` placeholder into this measured report.

    Returns True when a row was upgraded (caller must not insert). A measured row
    is never rewritten; a duplicate client `run_key` is handled by the INSERT.
    """
    cur = await tx.conn.execute(
        "UPDATE account_usage SET account_id = ?, agent_id = ?, input_tokens = ?,"
        " output_tokens = ?, cached_input_tokens = ?, cost_usd = ?, model = ?,"
        " engine = ?, role = ?, at = ?, run_key = ?, measurement = 'measured',"
        " unmeasured_reason = NULL"
        " WHERE id = ("
        "  SELECT u.id FROM account_usage u"
        "  WHERE u.task_id = ? AND u.attempt = ?"
        "   AND u.measurement = 'not_reported' AND substr(u.run_key, 1, 3) = ?"
        "   AND (SELECT count(*) FROM account_usage x"
        "        WHERE x.task_id = ? AND x.attempt = ?) = 1"
        " )",
        (
            account_id,
            agent_id,
            input_tokens,
            output_tokens,
            cached_input_tokens,
            cost_usd,
            model,
            engine,
            role,
            at,
            run_key,
            task_id,
            attempt,
            PLACEHOLDER_PREFIX,
            task_id,
            attempt,
        ),
    )
    return cur.rowcount > 0


async def ensure_attempt_outcome(
    tx: Tx,
    *,
    task_id: str,
    attempt: int,
    agent_id: str | None,
    claim_id: int,
    claimed_at: str | None,
    reason: str,
) -> None:
    """Write the attempt's `not_reported` placeholder when it has no usage row yet.

    Same transaction that takes the attempt out of `claimed` (account usage 3.10).
    """
    async with tx.conn.execute(
        "SELECT 1 FROM account_usage WHERE task_id = ? AND attempt = ? LIMIT 1",
        (task_id, attempt),
    ) as cur:
        if await cur.fetchone() is not None:
            return
    account_id: str | None = None
    role: str | None = None
    if agent_id is not None:
        end = ts(tx.now)
        start = claimed_at or end
        async with tx.conn.execute(
            "SELECT account_id FROM account_leases"
            " WHERE agent_id = ? AND role = 'engine' AND leased_at <= ?"
            " AND (released_at IS NULL OR released_at >= ?)"
            " ORDER BY leased_at DESC LIMIT 1",
            (agent_id, end, start),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            account_id = row[0]
            role = "engine"
    run_key = f"{PLACEHOLDER_PREFIX}{task_id}:{attempt}:{claim_id}"
    await tx.conn.execute(
        "INSERT INTO account_usage (account_id, agent_id, task_id, attempt,"
        " input_tokens, output_tokens, cached_input_tokens, cost_usd, model, engine,"
        " role, at, run_key, measurement, unmeasured_reason)"
        " VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, 'not_reported',"
        " ?) ON CONFLICT DO NOTHING",
        (account_id, agent_id, task_id, attempt, role, ts(tx.now), run_key, reason),
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
    reasons: dict[str, dict[str, int]] = defaultdict(dict)
    roles: dict[str, dict[str, int]] = defaultdict(dict)
    for target, column, only_unmeasured in (
        (reasons, "unmeasured_reason", True),
        (roles, "role", False),
    ):
        async with conn.execute(
            f"SELECT account_id, coalesce({column}, 'unknown'), count(*)"  # noqa: S608
            " FROM account_usage WHERE at >= ? AND at < ?"
            + (" AND measurement = 'not_reported'" if only_unmeasured else "")
            + " GROUP BY 1, 2",
            (ts(start), ts(end)),
        ) as cur:
            for account_id, key, n in await cur.fetchall():
                target[account_id][key] = n
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
                measured_runs=int(v[4]) - int(v[5]),
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
                    measured_runs=sum(b.measured_runs for b in buckets),
                    unmeasured_reasons=reasons.get(account_id, {}),
                    runs_by_role=roles.get(account_id, {}),
                ),
                buckets=buckets,
            )
        )
    return out


# -- quota status ---------------------------------------------------------------

STALE_AFTER = timedelta(hours=1)  # a provider snapshot older than this is stale

_POLLED_BY: dict[str, QuotaCollector] = {
    codex_auth.PROVIDER: "codex_rate_limits",
    claude_auth.PROVIDER: "claude_oauth_usage",
    cursor_auth.PROVIDER: "cursor_dashboard",
    CUSTOM_PROVIDER: "openrouter_key",
}
_PROBE_REASONS: dict[str, QuotaStatusReason] = {
    "failed": "probe_failed",
    "skipped": "probe_skipped",
    "rate_limited": "probe_rate_limited",
}
_LOGIN_PROVIDERS = frozenset({codex_auth.PROVIDER, claude_auth.PROVIDER})
Source = Literal["codex", "claude", "cursor", "openrouter"]


def collector(provider: str, source: str) -> QuotaCollector | None:
    """Who produced a snapshot: a worker's report, or the provider's own readout."""
    return "worker_report" if source == "reported" else _POLLED_BY.get(provider)


def quota_source(provider: str, kind: str, config: dict | None = None) -> Source | None:
    """The read-only source Argus can poll for this account, or None when there is
    none (an admin-only API, a local model). Whether an API key can be read is
    the credential's to say (`api_key_account` when it holds no login)."""
    if provider == codex_auth.PROVIDER:
        return "codex"
    if provider == claude_auth.PROVIDER:
        return "claude"
    if provider == cursor_auth.PROVIDER and kind == "subscription":
        return "cursor"  # an imported login; an API key is the Admin API's business
    if provider == CUSTOM_PROVIDER and (config or {}).get("label") == OPENROUTER:
        return "openrouter"
    return None


def no_source_reason(provider: str, kind: str) -> QuotaStatusReason:
    """Why an account with no pollable source has no provider figure."""
    if provider == "cursor":
        return "admin_key_required"
    if provider == "local":
        return "local_no_quota"
    if kind == "api_key" and provider in _LOGIN_PROVIDERS:
        return "api_key_account"
    return "no_provider_source"


def snapshot_is_fresh(q: QuotaWindow, now: datetime) -> bool:
    """Observed recently, and its window has not already reset."""
    if now - q.observed_at > STALE_AFTER:
        return False
    return q.resets_at is None or q.resets_at > now


async def record_probe(
    tx: Tx,
    account_id: str,
    status: str,
    reason: str | None,
    *,
    code: str | None = None,
    keep: bool = False,
) -> None:
    """The account's latest probe outcome (one row; `keep`: never replace one)."""
    on_conflict = (
        "NOTHING"
        if keep
        else "UPDATE SET status = excluded.status, reason = excluded.reason,"
        " code = excluded.code, at = excluded.at"
    )
    await tx.conn.execute(
        "INSERT INTO account_quota_probe (account_id, status, reason, code, at)"  # noqa: S608
        " SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM accounts WHERE id = ?)"
        f" ON CONFLICT (account_id) DO {on_conflict}",
        (
            account_id,
            status,
            reason[:200] if reason else None,
            code,
            ts(tx.now),
            account_id,
        ),
    )


async def last_probe(conn: aiosqlite.Connection, account_id: str) -> QuotaProbe | None:
    async with conn.execute(
        "SELECT status, code, reason, at FROM account_quota_probe WHERE account_id = ?",
        (account_id,),
    ) as cur:
        row = await cur.fetchone()
    return QuotaProbe.model_validate(dict(row)) if row else None


async def upsert_credit(
    tx: Tx, account_id: str, credit: "CreditReading", actor: str
) -> bool:
    """Replace the account's credit reading. One `account.quota` event when a
    figure changed (the observation time alone is not a change)."""
    before = await get_credit(tx.conn, account_id, "")
    new = (
        credit.limit,
        credit.remaining,
        credit.used,
        credit.limit_reset,
        credit.resets_at,
    )
    await tx.conn.execute(
        "INSERT INTO account_credit (account_id, unit, limit_amount,"
        " remaining_amount, used_amount, limit_reset, resets_at, free_tier,"
        " observed_at, source)"
        " VALUES (?, 'usd', ?, ?, ?, ?, ?, ?, ?, 'polled')"
        " ON CONFLICT (account_id) DO UPDATE SET limit_amount = excluded.limit_amount,"
        " remaining_amount = excluded.remaining_amount,"
        " used_amount = excluded.used_amount, limit_reset = excluded.limit_reset,"
        " resets_at = excluded.resets_at,"
        " free_tier = excluded.free_tier, observed_at = excluded.observed_at",
        (
            account_id,
            credit.limit,
            credit.remaining,
            credit.used,
            credit.limit_reset,
            ts(credit.resets_at) if credit.resets_at else None,
            None if credit.free_tier is None else int(credit.free_tier),
            ts(tx.now),
        ),
    )
    changed = (
        before is None
        or (
            before.limit,
            before.remaining,
            before.used,
            before.limit_reset,
            before.resets_at,
        )
        != new
    )
    if changed:
        await record(
            tx, actor, "account", account_id, "account.quota", {"windows": ["credit"]}
        )
    return changed


async def get_credit(
    conn: aiosqlite.Connection, account_id: str, provider: str
) -> AccountCredit | None:
    async with conn.execute(
        "SELECT unit, limit_amount, remaining_amount, used_amount, limit_reset,"
        " resets_at, free_tier, observed_at, source FROM account_credit WHERE account_id = ?",
        (account_id,),
    ) as cur:
        r = await cur.fetchone()
    if r is None:
        return None
    return AccountCredit(
        unit=r["unit"],
        limit=r["limit_amount"],
        remaining=r["remaining_amount"],
        used=r["used_amount"],
        limit_reset=r["limit_reset"],
        resets_at=r["resets_at"],
        free_tier=None if r["free_tier"] is None else bool(r["free_tier"]),
        observed_at=r["observed_at"],
        source=r["source"],
        collector=_POLLED_BY.get(provider),
    )


async def quota_status(
    conn: aiosqlite.Connection,
    account: dict,
    snapshots: list[QuotaWindow],
    now: datetime,
) -> QuotaStatus:
    """Do we know this account's quota? Provider snapshots first (`known` when all
    are fresh, else `stale`), then local estimates against `token_limits`;
    otherwise unknown, with the first reason that applies: the provider or account
    kind exposes none, the last probe's outcome, not probed yet. Having no usage
    rows says nothing about quota."""
    probe = await last_probe(conn, account["id"])
    credit = await get_credit(conn, account["id"], account["provider"])
    newest = max(snapshots, key=lambda q: q.observed_at) if snapshots else None

    def status(
        state: Literal["known", "stale", "unknown"],
        reason: QuotaStatusReason | None = None,
        windows: list[QuotaStatusWindow] | None = None,
    ) -> QuotaStatus:
        seen = [q.observed_at for q in snapshots] + (
            [credit.observed_at] if credit else []
        )
        return QuotaStatus(
            state=state,
            reason=reason,
            windows=windows or [],
            observed_at=max(seen) if seen else None,
            collector=collector(account["provider"], newest.source)
            if newest
            else credit.collector
            if credit
            else None,
            last_probe=probe,
            credit=credit,
        )

    credit_fresh = credit is None or now - credit.observed_at <= STALE_AFTER
    if (
        (snapshots or credit)
        and credit_fresh
        and all(snapshot_is_fresh(q, now) for q in snapshots)
    ):
        return status("known")
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
        return status("known", windows=windows)
    if snapshots or credit:
        return status("stale")
    config = json.loads(account["config"]) if account.get("config") else None
    if account["kind"] == "api_key" and account["provider"] in _LOGIN_PROVIDERS:
        return status("unknown", "api_key_account")
    if quota_source(account["provider"], account["kind"], config) is None:
        return status("unknown", no_source_reason(account["provider"], account["kind"]))
    if probe is not None and probe.status in _PROBE_REASONS:
        return status("unknown", probe.code or _PROBE_REASONS[probe.status])
    return status("unknown", "not_yet_probed")
