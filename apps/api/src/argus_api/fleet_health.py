"""Fleet health for the dashboard Fleet view (docs/specs/fleet-health.spec.md).

Read-only, one snapshot. Every query is bounded by the window through an index
(`tasks_finished`, `events_kind_at`, `events_subject`, `tasks_state`); none scans
the tasks or events history. No metric is ever 0 for "no data": `None` means
unknown, and a statistic with fewer than `MIN_SAMPLES` samples is `None`.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from argus_core.fleet import InstanceState
from argus_core.tasks import TERMINAL, RetryClass, TaskState, retry_class
from pydantic import BaseModel, ConfigDict, Field

from argus_api.db import Database, ts, utcnow
from argus_api.overview import Counts, _filled

MIN_SAMPLES = 5
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 720  # 30 days
MAX_ROWS = 5000  # newest rows per bounded fetch; `truncated` says when it bit
EXAMPLES = 5
EXCERPT_CHARS = 200
# P5 draft target: median session_lost → next claim under 5 minutes. Samples at or
# above this are counted as outliers; they still enter the median when n is enough.
RECOVERY_TARGET_S = 300.0

REASONS = (
    "auth_missing",
    "not_installed",
    "lease_expired",
    "gate_failed",
    "timeout",
    "stalled",
    "worker_fail",
    "channel_error",
    "provider_failure",
    "quota",
    "account_unavailable",
    "agent_ended",
    "session_lost",
    "env_oom",
    "other",
)

# Finished tasks in the window with their first and last claim. `updated_at` is
# when the task reached `succeeded`/`failed` (nothing edits a finished task).
FINISHED_SQL = (
    "SELECT t.id, t.state, t.created_at, t.updated_at,"
    " (SELECT min(e.at) FROM events e WHERE e.subject_type = 'task'"
    "   AND e.subject_id = t.id AND e.kind = 'task.claim') AS first_claim,"
    " (SELECT max(e.at) FROM events e WHERE e.subject_type = 'task'"
    "   AND e.subject_id = t.id AND e.kind = 'task.claim') AS last_claim"
    " FROM tasks t WHERE t.state IN ('succeeded', 'failed')"
    " AND t.updated_at >= ? AND t.updated_at <= ?"
    " ORDER BY t.updated_at DESC LIMIT ?"
)
FAILURES_SQL = (
    "SELECT e.id, e.at, e.subject_id, e.data, t.title,"
    " (SELECT max(c.at) FROM events c WHERE c.subject_type = 'task'"
    "   AND c.subject_id = e.subject_id AND c.kind = 'task.claim'"
    "   AND c.at <= e.at) AS claim_at FROM events e"
    " JOIN tasks t ON t.id = e.subject_id"
    " WHERE e.kind IN ('task.fail', 'task.expire', 'task.block')"
    # A block is a failure only when an OOM-killed attempt promoted (3.2.1).
    " AND (e.kind <> 'task.block' OR json_extract(e.data, '$.reason') = 'env_oom')"
    " AND e.at >= ? AND e.at <= ?"
    " ORDER BY e.at DESC, e.id DESC LIMIT ?"
)
ATTEMPTS_SQL = (
    "SELECT kind, count(*) FROM events WHERE kind IN ('task.claim', 'task.complete')"
    " AND at >= ? AND at <= ? GROUP BY kind"
)
# Measured tokens/cost and attempt coverage for a set of tasks (JSON array of ids).
# Unknown is never zero: token/cost sums cover measured rows only; attempt counts
# come from distinct (task, attempt) pairs with any usage row (account usage 3.10).
USAGE_SQL = (
    "SELECT u.task_id,"
    # Tokens only when a measured row actually reported some; cost-only measured
    # (legal: tokens OR cost) must stay NULL, never coalesce into an invented 0.
    " sum(CASE WHEN u.measurement = 'measured'"
    "     AND (u.input_tokens IS NOT NULL OR u.output_tokens IS NOT NULL)"
    "     THEN coalesce(u.input_tokens, 0) + coalesce(u.output_tokens, 0) END)"
    " AS tokens,"
    " sum(CASE WHEN u.measurement = 'measured' THEN u.cost_usd END) AS cost,"
    " (SELECT count(*) FROM ("
    "   SELECT attempt FROM account_usage a"
    "   WHERE a.task_id = u.task_id AND a.attempt IS NOT NULL GROUP BY a.attempt"
    " )) AS attempts,"
    " (SELECT count(*) FROM ("
    "   SELECT attempt FROM account_usage a"
    "   WHERE a.task_id = u.task_id AND a.attempt IS NOT NULL"
    "   GROUP BY a.attempt HAVING max(a.measurement = 'measured')"
    " )) AS measured_attempts"
    " FROM account_usage u"
    " WHERE u.task_id IN (SELECT value FROM json_each(?)) GROUP BY u.task_id"
)
HEARTBEAT_SQL = (
    "SELECT i.pool, max(a.last_heartbeat_at) FROM agents a"
    " JOIN instances i ON i.id = a.instance_id"
    " WHERE a.state IN ('active', 'draining') GROUP BY i.pool"
)
# session_lost fails in the window, each paired by event id (the monotonic order;
# `at` can step back with the clock) with the task's first claim after it and
# before its next attempt end, plus the task's current state. Death time is not
# stored: start is the fail event's `at` (observed lost). Requeue itself is the
# same event when data.to = 'ready' (failure classes 3.2).
SESSION_LOST_SQL = (
    "WITH lost AS MATERIALIZED ("
    "  SELECT e.id, e.at, e.subject_id, e.data,"
    "   (SELECT f.id FROM events f WHERE f.subject_type = 'task'"
    "     AND f.subject_id = e.subject_id AND f.kind IN ('task.fail', 'task.expire')"
    "     AND f.id > e.id ORDER BY f.id LIMIT 1) AS next_end_id"
    "  FROM events e WHERE e.kind = 'task.fail' AND e.at >= ? AND e.at <= ?"
    "  AND json_extract(e.data, '$.reason') = 'session_lost'"
    "  ORDER BY e.at DESC, e.id DESC LIMIT ?"
    "), paired AS MATERIALIZED ("
    "  SELECT l.*, (SELECT c.id FROM events c WHERE c.subject_type = 'task'"
    "   AND c.subject_id = l.subject_id AND c.kind = 'task.claim' AND c.id > l.id"
    "   AND (l.next_end_id IS NULL OR c.id < l.next_end_id)"
    "   ORDER BY c.id LIMIT 1) AS next_claim_id FROM lost l"
    ")"
    " SELECT p.id, p.at, p.subject_id, p.data, p.next_end_id, p.next_claim_id,"
    " c.at AS next_claim_at, t.state AS task_state FROM paired p"
    " LEFT JOIN events c ON c.id = p.next_claim_id"
    " LEFT JOIN tasks t ON t.id = p.subject_id"
    " ORDER BY p.at DESC, p.id DESC"
)
# A requeued task with no paired claim is still waiting in these states; a terminal
# one ended unclaimed; any other state (or no row) means a claim event is missing.
WAITING_STATES = frozenset({TaskState.READY, TaskState.PENDING, TaskState.BLOCKED})


class Provenance(BaseModel):
    window_hours: int
    source: str


class PoolHealth(BaseModel):
    name: str
    status: str
    desired: int
    live: int  # instances not terminated
    running: int
    last_heartbeat_at: datetime | None  # None: no live agent has reported ("no data")
    oom_streak: int = 0  # consecutive OOM-killed sessions (failure classes 3.2.1)
    source: str = "pools, instances, agents.last_heartbeat_at"


class Durations(Provenance):
    n: int
    p50: float | None  # seconds
    p90: float | None
    mean: float | None


class Outcomes(Provenance):
    succeeded: int
    failed: int
    total: int
    success_rate: float | None
    failure_rate: float | None


class FailureExample(BaseModel):
    task_id: str
    title: str
    error: str  # truncated to EXCERPT_CHARS
    at: datetime


class FailureGroup(BaseModel):
    reason: str
    retry_class: RetryClass  # failure classes 3.1
    count: int
    # Time the failed attempts held a claim (claim to failure or expiry); None when
    # no event in the group has a claim before it.
    lost_seconds: float | None = None
    examples: list[FailureExample]


class Failures(Provenance):
    total: int
    groups: list[FailureGroup]
    by_class: dict[RetryClass, int]
    truncated: bool


class Yield(Provenance):
    """What the fleet got for what it spent (worker efficiency draft, P8)."""

    attempts: int  # claims in the window
    completions: int  # attempts that handed in a result
    attempt_success_rate: float | None
    finished_tasks: int
    attempts_per_finished_task: float | None
    # Coverage over the finished cohort's attempts with a usage row (account usage
    # 3.10). `usage_tasks` had any row; `measured_tasks` have every attempt
    # measured. Token/cost means use measured tasks only and never count unknown
    # as zero (`None` under MIN_SAMPLES).
    usage_tasks: int
    measured_tasks: int = 0
    measured_attempts: int = 0
    not_reported_attempts: int = 0
    tokens_per_succeeded: float | None
    tokens_per_failed: float | None
    cost_per_succeeded: float | None


class RecoveryExample(BaseModel):
    """One auditable synthetic sample: source fail (+ claim) event ids."""

    task_id: str
    fail_event_id: int
    claim_event_id: int | None
    observed_lost_at: datetime  # task.fail.at; not a death timestamp
    next_claim_at: datetime | None
    latency_s: float | None
    outcome: str  # measured | pending | ended_unclaimed | not_requeued | anomalous


class DeadSessionRecovery(Provenance):
    """session_lost observed → next claim (worker efficiency draft, P5 measure).

    Death time is not in events. Fail and requeue are the same `task.fail` when
    `to=ready`. Latency is therefore observation → the claim paired by event id,
    never a fabricated death→requeue delta. Percentiles need `MIN_SAMPLES`
    measured rows.
    """

    failure_class: str = "session"
    reason: str = "session_lost"
    denominator: int  # session_lost fails in the window (bounded fetch)
    measured: int
    pending: int  # requeued, no paired claim, task still waiting
    # Pending with age (now - observed_lost_at) >= target_s. A lower bound only:
    # never in percentiles, mean or outliers.
    pending_overdue: int
    ended_unclaimed: int  # requeued, no paired claim, task now terminal
    not_requeued: int  # to=failed (attempts exhausted) or unexpected to
    anomalous: int  # clock rollback / missing claim event / unreadable
    coverage: float | None  # measured / denominator; None when denominator is 0
    n: int  # == measured; kept for Durations-shaped clients
    p50: float | None
    p90: float | None
    mean: float | None
    target_s: float = RECOVERY_TARGET_S
    outliers: int  # measured with latency_s >= target_s
    truncated: bool
    examples: list[RecoveryExample]


class TasksByState(BaseModel):
    source: str
    counts: Counts


class FleetHealth(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    as_of: datetime
    window_hours: int
    pools: list[PoolHealth]
    tasks_by_state: TasksByState
    queue_time: Durations
    run_time: Durations
    outcomes: Outcomes
    failures: Failures
    yield_: Yield = Field(alias="yield")
    dead_session_recovery: DeadSessionRecovery
    truncated: bool  # the finished-task fetch hit MAX_ROWS (samples are the newest)


def classify_failure(reason: str | None, error: str | None) -> str:
    """The one place a failure becomes a `REASONS` entry.

    A reason the system itself set (`lease_expired`, `timeout`, `agent_ended`, or a
    worker's own code) wins. A worker's generic `worker_fail` (or no reason) is
    refined by the error text: `auth_missing`, `not_installed` and `gate_failed`
    are matched by keyword, ignoring case and treating spaces or hyphens as `_`. Anything else is `other`.
    """
    if reason and reason != "worker_fail":
        return reason if reason in REASONS else "other"
    text = re.sub(r"[\s-]+", "_", (error or "").lower())
    for known in ("auth_missing", "not_installed", "gate_failed"):
        if known in text:
            return known
    return "worker_fail" if reason else "other"


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (the common "type 7" quantile)."""
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _durations(values: list[float], window: int, source: str) -> Durations:
    n = len(values)
    if n < MIN_SAMPLES:
        return Durations(
            window_hours=window, source=source, n=n, p50=None, p90=None, mean=None
        )
    ordered = sorted(values)
    return Durations(
        window_hours=window,
        source=source,
        n=n,
        p50=percentile(ordered, 0.5),
        p90=percentile(ordered, 0.9),
        mean=sum(ordered) / n,
    )


def _seconds(start: str, end: str) -> float:
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return max(0.0, delta.total_seconds())


def _rate(n: int, total: int) -> float | None:
    return n / total if total >= MIN_SAMPLES else None


async def fleet_health(
    db: Database, window_hours: int = DEFAULT_WINDOW_HOURS, now: datetime | None = None
) -> FleetHealth:
    now = now or utcnow()
    since, until = ts(now - timedelta(hours=window_hours)), ts(now)
    async with db.read() as conn:
        await conn.execute("BEGIN")  # one snapshot for every query below
        try:

            async def rows(sql: str, *params: Any) -> list[Any]:
                async with conn.execute(sql, params) as cur:
                    return list(await cur.fetchall())

            by_state = _filled(
                TaskState,
                await rows("SELECT state, count(*) FROM tasks GROUP BY state"),
            )
            pools = await rows(
                "SELECT name, status, desired_replicas, oom_streak FROM pools ORDER BY name"
            )
            instances = {
                (pool, state): n
                for pool, state, n in await rows(
                    "SELECT pool, state, count(*) FROM instances GROUP BY pool, state"
                )
            }
            heartbeats = dict(await rows(HEARTBEAT_SQL))
            finished = await rows(FINISHED_SQL, since, until, MAX_ROWS)
            failures = await rows(FAILURES_SQL, since, until, MAX_ROWS)
            session_lost = await rows(SESSION_LOST_SQL, since, until, MAX_ROWS)
            attempt_counts = dict(await rows(ATTEMPTS_SQL, since, until))
            usage = {
                r[0]: (r[1], r[2], r[3], r[4])
                for r in await rows(USAGE_SQL, json.dumps([r["id"] for r in finished]))
            }
        finally:
            await conn.execute("COMMIT")

    pool_items = [
        PoolHealth(
            name=name,
            status=status,
            desired=desired,
            live=sum(
                n
                for (p, s), n in instances.items()
                if p == name and s != InstanceState.TERMINATED
            ),
            running=instances.get((name, InstanceState.RUNNING), 0),
            last_heartbeat_at=(
                datetime.fromisoformat(heartbeats[name])
                if heartbeats.get(name)
                else None
            ),
            oom_streak=oom_streak,
        )
        for name, status, desired, oom_streak in pools
    ]
    queue = [
        _seconds(r["created_at"], r["first_claim"])
        for r in finished
        if r["first_claim"]
    ]
    run = [
        _seconds(r["last_claim"], r["updated_at"]) for r in finished if r["last_claim"]
    ]
    succeeded = sum(1 for r in finished if r["state"] == TaskState.SUCCEEDED)
    total = len(finished)
    window = window_hours
    return FleetHealth(
        as_of=now,
        window_hours=window,
        pools=pool_items,
        tasks_by_state=TasksByState(source="tasks", counts=by_state),
        queue_time=_durations(queue, window, "tasks.created_at, events task.claim"),
        run_time=_durations(run, window, "events task.claim, tasks.updated_at"),
        outcomes=Outcomes(
            window_hours=window,
            source="tasks",
            succeeded=succeeded,
            failed=total - succeeded,
            total=total,
            success_rate=_rate(succeeded, total),
            failure_rate=_rate(total - succeeded, total),
        ),
        failures=_group_failures(failures, window),
        yield_=_yield(window, attempt_counts, finished, usage),
        dead_session_recovery=_dead_session_recovery(session_lost, window, now),
        truncated=total >= MAX_ROWS,
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if len(values) >= MIN_SAMPLES else None


def _fully_measured(row: tuple[int | None, float | None, int, int]) -> bool:
    """True when the task has measured figures and no not_reported attempt."""
    tokens, cost, attempts, measured = row
    if attempts > measured:
        return False
    return tokens is not None or cost is not None or measured > 0


def _yield(
    window: int,
    counts: dict[str, int],
    finished: list[Any],
    usage: dict[str, tuple[int | None, float | None, int, int]],
) -> Yield:
    attempts, completions = counts.get("task.claim", 0), counts.get("task.complete", 0)
    # Means only over fully measured tasks (never count an unreported attempt as 0).
    ok = [
        usage[r["id"]]
        for r in finished
        if r["state"] == "succeeded"
        and r["id"] in usage
        and _fully_measured(usage[r["id"]])
    ]
    bad = [
        usage[r["id"]]
        for r in finished
        if r["state"] != "succeeded"
        and r["id"] in usage
        and _fully_measured(usage[r["id"]])
    ]
    measured_attempts = sum(u[3] for u in usage.values())
    attempt_rows = sum(u[2] for u in usage.values())
    return Yield(
        window_hours=window,
        source="events task.claim, task.complete; tasks; account_usage",
        attempts=attempts,
        completions=completions,
        attempt_success_rate=_rate(completions, attempts),
        finished_tasks=len(finished),
        attempts_per_finished_task=(
            attempts / len(finished) if len(finished) >= MIN_SAMPLES else None
        ),
        usage_tasks=sum(1 for r in finished if r["id"] in usage),
        measured_tasks=len(ok) + len(bad),
        measured_attempts=measured_attempts,
        not_reported_attempts=max(0, attempt_rows - measured_attempts),
        tokens_per_succeeded=_mean([float(t) for t, _, _, _ in ok if t is not None]),
        tokens_per_failed=_mean([float(t) for t, _, _, _ in bad if t is not None]),
        cost_per_succeeded=_mean([c for _, c, _, _ in ok if c is not None]),
    )


def _group_failures(rows: list[Any], window: int) -> Failures:
    groups: dict[str, FailureGroup] = {}
    for row in rows:  # newest first, so the first examples are the most recent
        data = json.loads(row["data"])
        reason = classify_failure(data.get("reason"), data.get("error"))
        group = groups.setdefault(
            reason,
            FailureGroup(
                reason=reason, retry_class=retry_class(reason), count=0, examples=[]
            ),
        )
        group.count += 1
        if row["claim_at"]:
            group.lost_seconds = (group.lost_seconds or 0.0) + _seconds(
                row["claim_at"], row["at"]
            )
        if len(group.examples) < EXAMPLES:
            group.examples.append(
                FailureExample(
                    task_id=row["subject_id"],
                    title=row["title"],
                    error=(data.get("error") or "")[:EXCERPT_CHARS],
                    at=datetime.fromisoformat(row["at"]),
                )
            )
    ordered = sorted(groups.values(), key=lambda g: (-g.count, g.reason))
    return Failures(
        window_hours=window,
        source="events task.fail, task.expire",
        total=len(rows),
        groups=ordered,
        by_class={
            c: sum(g.count for g in ordered if g.retry_class is c) for c in RetryClass
        },
        truncated=len(rows) >= MAX_ROWS,
    )


def classify_recovery_row(row: Any) -> RecoveryExample:
    """Build one synthetic recovery sample from a SESSION_LOST_SQL row.

    Outcomes (spec 3, dead-session recovery): measured (requeued, paired claim,
    latency >= 0), pending (requeued, no paired claim, task still waiting),
    ended_unclaimed (requeued, no paired claim, task now terminal), not_requeued
    (terminal or unexpected `to`), anomalous (unreadable payload or time, negative
    latency, an attempt end without a claim, or a claim event that must exist).
    """
    task_id, fail_id = str(row["subject_id"]), int(row["id"])
    claim_id = int(row["next_claim_id"]) if row["next_claim_id"] is not None else None

    def sample(
        outcome: str,
        observed: datetime | None = None,
        claim_at: datetime | None = None,
        latency: float | None = None,
    ) -> RecoveryExample:
        return RecoveryExample(
            task_id=task_id,
            fail_event_id=fail_id,
            claim_event_id=claim_id,
            observed_lost_at=observed or datetime.min.replace(tzinfo=UTC),
            next_claim_at=claim_at,
            latency_s=latency,
            outcome=outcome,
        )

    try:
        observed = _aware(row["at"])
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):  # JSONDecodeError is a ValueError
        return sample("anomalous", _iso(row["at"]))
    try:
        claim_at = _aware(row["next_claim_at"]) if claim_id is not None else None
    except (TypeError, ValueError):
        return sample("anomalous", observed)
    if not isinstance(data, dict) or data.get("to") != "ready":
        return sample("not_requeued", observed, claim_at)
    if claim_at is not None:
        latency = (claim_at - observed).total_seconds()
        # A claim with a higher id but an earlier `at`: the clock stepped back.
        outcome = "measured" if latency >= 0 else "anomalous"
        return sample(outcome, observed, claim_at, latency)
    if row["next_end_id"] is not None:  # the next attempt ended with no claim
        return sample("anomalous", observed)
    if row["task_state"] in WAITING_STATES:
        return sample("pending", observed)
    if row["task_state"] in TERMINAL:
        return sample("ended_unclaimed", observed)
    return sample("anomalous", observed)


def _aware(value: Any) -> datetime:
    """Parse an event time; one without a zone cannot be compared, so it is invalid."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"time without zone: {value!r}")
    return parsed


def _iso(value: Any) -> datetime | None:
    try:
        return _aware(value)
    except (TypeError, ValueError):
        return None


def _dead_session_recovery(
    rows: list[Any], window: int, now: datetime
) -> DeadSessionRecovery:
    samples = [classify_recovery_row(r) for r in rows]
    pending = [s for s in samples if s.outcome == "pending"]
    measured = [s for s in samples if s.outcome == "measured"]
    latencies = [s.latency_s for s in measured if s.latency_s is not None]
    n = len(latencies)
    denom = len(samples)
    stats = _durations(
        latencies,
        window,
        "events task.fail reason=session_lost → next task.claim",
    )
    return DeadSessionRecovery(
        window_hours=window,
        source=stats.source,
        failure_class="session",
        reason="session_lost",
        denominator=denom,
        measured=n,
        pending=len(pending),
        # Censored: age is a lower bound of an unknown latency, never a sample.
        pending_overdue=sum(
            1
            for s in pending
            if (now - s.observed_lost_at).total_seconds() >= RECOVERY_TARGET_S
        ),
        ended_unclaimed=sum(1 for s in samples if s.outcome == "ended_unclaimed"),
        not_requeued=sum(1 for s in samples if s.outcome == "not_requeued"),
        anomalous=sum(1 for s in samples if s.outcome == "anomalous"),
        coverage=(n / denom) if denom else None,
        n=n,
        p50=stats.p50,
        p90=stats.p90,
        mean=stats.mean,
        target_s=RECOVERY_TARGET_S,
        outliers=sum(1 for v in latencies if v >= RECOVERY_TARGET_S),
        truncated=len(rows) >= MAX_ROWS,
        examples=[s for s in samples if s.outcome == "measured"][:EXAMPLES],
    )
