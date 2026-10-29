"""Dead-session recovery query cost (docs/specs/fleet-health.spec.md 3, "Query cost").

Builds a throwaway in-memory SQLite database from the real migrations, seeds N
`session_lost` fails of N tasks, each followed by a re-claim and then a
configurable number of post-claim `task.progress` events (optionally plus
unrelated events of other tasks), and times `SESSION_LOST_SQL` with the real
`MAX_ROWS`. The plan from `EXPLAIN QUERY PLAN` is captured next to the timing.

The subquery `f` (next `task.fail`/`task.expire`) has no upper bound from the next
claim, so it visits a task's own post-claim events until its next end or last
event; this harness measures that. Synthetic fixture, one machine: the figures
are not a bound and not production numbers. Nothing here asserts timing.

    uv run python apps/api/bench/dead_session_recovery_bench.py \
        [--fails 1000] [--post-claim 0 100 1000 5000] [--unrelated 0] [--runs 3] \
        [--out FILE]
"""

import argparse
import json
import os
import platform
import sqlite3
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argus_api.db.migrate import load_migrations
from argus_api.fleet_health import MAX_ROWS, SESSION_LOST_SQL

NOW = datetime(2026, 3, 1, tzinfo=UTC)
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"


def build(fails: int, post_claim: int, unrelated: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for m in load_migrations():
        conn.executescript(m.sql)
    stamp = NOW.strftime(STAMP)
    event_sql = (
        "INSERT INTO events (at, actor, subject_type, subject_id, kind, data)"
        " VALUES (?, 'system', 'task', ?, ?, ?)"
    )
    lost = json.dumps({"reason": "session_lost", "from": "claimed", "to": "ready"})
    for i in range(fails):
        task = f"t{i}"
        at = (NOW - timedelta(seconds=fails - i)).strftime(STAMP)
        conn.execute(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project)"
            " VALUES (?, 'generic', ?, '', '[]', '{}', '[]', 0, 'succeeded', 0,"
            " 'none', 60, 3, 'bench', ?, ?, 'global')",
            (task, task, stamp, stamp),
        )
        conn.execute(event_sql, (at, task, "task.fail", lost))
        conn.execute(event_sql, (at, task, "task.claim", '{"attempt": 2}'))
        conn.executemany(event_sql, [(at, task, "task.progress", "{}")] * post_claim)
    conn.executemany(
        event_sql,
        [(stamp, f"other{i % 50}", "task.progress", "{}") for i in range(unrelated)],
    )
    conn.commit()
    return conn


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def ram_gb() -> float | None:
    try:
        return round(
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1
        )
    except (ValueError, OSError, AttributeError):
        return None


def measure(fails: int, post_claim: int, unrelated: int, runs: int) -> dict:
    conn = build(fails, post_claim, unrelated)
    since = (NOW - timedelta(days=1)).strftime(STAMP)
    until = (NOW + timedelta(days=1)).strftime(STAMP)
    args = (since, until, MAX_ROWS)
    plan = [
        row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + SESSION_LOST_SQL, args)
    ]
    times, rows = [], 0
    for _ in range(runs):
        start = time.perf_counter()
        rows = len(conn.execute(SESSION_LOST_SQL, args).fetchall())
        times.append((time.perf_counter() - start) * 1000)
    events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conn.close()
    return {
        "post_claim": post_claim,
        "events": events,
        "rows": rows,
        "median_ms": round(statistics.median(times), 1),
        "plan": plan,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fails", type=int, default=1000)
    p.add_argument("--post-claim", type=int, nargs="+", default=[0, 100, 1000, 5000])
    p.add_argument("--unrelated", type=int, default=0)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    report = {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "cpu": cpu_model(),
        "cores": os.cpu_count(),
        "ram_gb": ram_gb(),
        "os": f"{platform.system()} {platform.release()}",
        "fails": a.fails,
        "runs": a.runs,
        "unrelated_events": a.unrelated,
        "limits": {"MAX_ROWS": MAX_ROWS},
        "results": [measure(a.fails, n, a.unrelated, a.runs) for n in a.post_claim],
    }
    text = json.dumps(report, indent=2)
    if a.out:
        a.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
