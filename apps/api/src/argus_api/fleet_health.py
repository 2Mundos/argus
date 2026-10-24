"""Fleet health for the dashboard Fleet view (docs/specs/fleet-health.spec.md).

Read-only, one snapshot. Every query is bounded by the window through an index
(`tasks_finished`, `events_kind_at`, `events_subject`, `tasks_state`); none scans
the tasks or events history. No metric is ever 0 for "no data": `None` means
unknown, and a statistic with fewer than `MIN_SAMPLES` samples is `None`.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any

from argus_core.fleet import InstanceState
from argus_core.tasks import TaskState
from pydantic import BaseModel

from argus_api.db import Database, ts, utcnow
from argus_api.overview import Counts, _filled

MIN_SAMPLES = 5
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 720  # 30 days
MAX_ROWS = 5000  # newest rows per bounded fetch; `truncated` says when it bit
EXAMPLES = 5
EXCERPT_CHARS = 200

REASONS = (
    "auth_missing",
    "not_installed",
    "lease_expired",
    "gate_failed",
    "timeout",
    "worker_fail",
    "channel_error",
    "provider_failure",
    "agent_ended",
    "other",
)

# Finished tasks in the window with their first and last claim. `updated_at` is
# when the task reached `succeeded`/`failed` (nothing edits a finished task).
FINISHED_SQL = (
    "SELECT t.state, t.created_at, t.updated_at,"
    " (SELECT min(e.at) FROM events e WHERE e.subject_type = 'task'"
    "   AND e.subject_id = t.id AND e.kind = 'task.claim') AS first_claim,"
    " (SELECT max(e.at) FROM events e WHERE e.subject_type = 'task'"
    "   AND e.subject_id = t.id AND e.kind = 'task.claim') AS last_claim"
    " FROM tasks t WHERE t.state IN ('succeeded', 'failed')"
    " AND t.updated_at >= ? AND t.updated_at <= ?"
    " ORDER BY t.updated_at DESC LIMIT ?"
)
FAILURES_SQL = (
    "SELECT e.id, e.at, e.subject_id, e.data, t.title FROM events e"
    " JOIN tasks t ON t.id = e.subject_id"
    " WHERE e.kind IN ('task.fail', 'task.expire') AND e.at >= ? AND e.at <= ?"
    " ORDER BY e.at DESC, e.id DESC LIMIT ?"
)
HEARTBEAT_SQL = (
    "SELECT i.pool, max(a.last_heartbeat_at) FROM agents a"
    " JOIN instances i ON i.id = a.instance_id"
    " WHERE a.state IN ('active', 'draining') GROUP BY i.pool"
)


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
    count: int
    examples: list[FailureExample]


class Failures(Provenance):
    total: int
    groups: list[FailureGroup]
    truncated: bool


class TasksByState(BaseModel):
    source: str
    counts: Counts


class FleetHealth(BaseModel):
    as_of: datetime
    window_hours: int
    pools: list[PoolHealth]
    tasks_by_state: TasksByState
    queue_time: Durations
    run_time: Durations
    outcomes: Outcomes
    failures: Failures
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
                "SELECT name, status, desired_replicas FROM pools ORDER BY name"
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
        )
        for name, status, desired in pools
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
        truncated=total >= MAX_ROWS,
    )


def _group_failures(rows: list[Any], window: int) -> Failures:
    groups: dict[str, FailureGroup] = {}
    for row in rows:  # newest first, so the first examples are the most recent
        data = json.loads(row["data"])
        reason = classify_failure(data.get("reason"), data.get("error"))
        group = groups.setdefault(
            reason, FailureGroup(reason=reason, count=0, examples=[])
        )
        group.count += 1
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
        truncated=len(rows) >= MAX_ROWS,
    )
