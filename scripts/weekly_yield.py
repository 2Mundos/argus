#!/usr/bin/env python3
"""Weekly yield report: the premises-review yield figures for one window.

    scripts/weekly_yield.py --db copy-of-argus.db --since 2026-11-04 --until 2026-11-11
        [--merged merged.json] [--json]

Reads a copy of the database read-only (`mode=ro&immutable=1`); it never writes.
Definitions follow docs/drafts/2026-11-11.premises-review.draft.md and the baseline
queries of docs/drafts/2026-10-24.worker-efficiency.draft.md:

- Window tasks: tasks created from `--since` 00:00 UTC through the end of `--until`
  (both UTC dates, inclusive), project `global` excluded.
- First-claim success: each task's first `task.claim` is paired with the next
  `task.fail`, `task.complete`, `task.expire` or `task.released` for the same task in
  event id order. Success is `task.complete`. Denominator: window tasks with a first
  claim that has such an outcome (a claim still in flight is not counted).
- Time to a merged PR: from task creation to the merge time. The database holds no
  merge time (it records `pr_opened` progress events with the PR URL only), so
  `--merged` supplies it: a JSON object mapping a task id, or the task's PR URL, to
  an ISO-8601 merge time. Tasks without a merge time are censored: median and p90 use
  the merged tasks only and the denominator says how many. Without `--merged`, or with
  no merged task, the values are "unknown".
- Decisions per succeeded task: decisions of any state whose subject is a window task
  in state `succeeded`, divided by that count of tasks. Unknown when none succeeded.
- Environment block share: window tasks with a `task.block` event whose reason starts
  with `env_`, over window tasks.

An undefined value is "unknown" (null in JSON), never 0.
"""

import argparse
import json
import math
import sqlite3
import statistics
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

OUTCOMES = ("task.fail", "task.complete", "task.expire", "task.released")


def _parse(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _bounds(since: date, until: date) -> tuple[str, str]:
    """Half-open [start, end) as text prefixes of the fixed-width UTC timestamps."""
    return (
        f"{since.isoformat()}T00:00:00",
        f"{(until + timedelta(days=1)).isoformat()}T00:00:00",
    )


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def report(
    db: sqlite3.Connection,
    since: date,
    until: date,
    merged: dict[str, str] | None = None,
) -> dict[str, Any]:
    start, end = _bounds(since, until)
    tasks = {
        row[0]: (row[1], row[2])
        for row in db.execute(
            "SELECT id, created_at, state FROM tasks WHERE project <> 'global'"
            " AND created_at >= ? AND created_at < ?",
            (start, end),
        )
    }
    first_claim_ok = first_claim_n = env_blocked = 0
    pr_urls: dict[str, str] = {}
    for task_id in tasks:
        events = db.execute(
            "SELECT kind, data FROM events WHERE subject_type = 'task' AND subject_id = ?"
            " ORDER BY id",
            (task_id,),
        ).fetchall()
        kinds = [k for k, _ in events]
        if "task.claim" in kinds:
            after = kinds[kinds.index("task.claim") + 1 :]
            outcome = next((k for k in after if k in OUTCOMES), None)
            if outcome:
                first_claim_n += 1
                first_claim_ok += outcome == "task.complete"
        for kind, data in events:
            payload = json.loads(data)
            if kind == "task.block" and str(payload.get("reason", "")).startswith(
                "env_"
            ):
                env_blocked += 1
                break
        for kind, data in events:
            payload = json.loads(data)
            if kind == "task.progress" and payload.get("kind") == "pr_opened":
                url = (payload.get("data") or {}).get("url")
                if url:
                    pr_urls[task_id] = url
                    break

    minutes: list[float] = []
    for task_id, (created, _) in tasks.items():
        at = (merged or {}).get(task_id) or (merged or {}).get(pr_urls.get(task_id, ""))
        if at:
            minutes.append((_parse(at) - _parse(created)).total_seconds() / 60)

    succeeded = [t for t, (_, state) in tasks.items() if state == "succeeded"]
    decisions = sum(
        db.execute(
            "SELECT count(*) FROM decisions WHERE subject_type = 'task' AND subject_id = ?",
            (t,),
        ).fetchone()[0]
        for t in succeeded
    )
    return {
        "window": {"from": since.isoformat(), "to": until.isoformat()},
        "tasks": len(tasks),
        "first_claim_success": {
            "value": _ratio(first_claim_ok, first_claim_n),
            "succeeded": first_claim_ok,
            "denominator": first_claim_n,
        },
        "minutes_to_merged_pr": {
            "median": round(statistics.median(minutes), 1) if minutes else None,
            "p90": round(_p90(minutes), 1) if minutes else None,
            "denominator": len(minutes),
            "merge_data": merged is not None,
        },
        "decisions_per_succeeded_task": {
            "value": round(decisions / len(succeeded), 3) if succeeded else None,
            "decisions": decisions,
            "denominator": len(succeeded),
        },
        "env_block_share": {
            "value": _ratio(env_blocked, len(tasks)),
            "blocked": env_blocked,
            "denominator": len(tasks),
        },
    }


def _show(value: object, unit: str = "") -> str:
    return "unknown" if value is None else f"{value}{unit}"


def render(r: dict[str, Any]) -> str:
    fc, mp = r["first_claim_success"], r["minutes_to_merged_pr"]
    dc, eb = r["decisions_per_succeeded_task"], r["env_block_share"]
    return "\n".join(
        [
            f"window: {r['window']['from']} to {r['window']['to']} (UTC, inclusive)",
            f"tasks: {r['tasks']} (project global excluded)",
            f"first-claim success: {_show(fc['value'])} ({fc['succeeded']}/{fc['denominator']})",
            f"minutes to merged PR, median: {_show(mp['median'])} (n={mp['denominator']})",
            f"minutes to merged PR, p90: {_show(mp['p90'])} (n={mp['denominator']})",
            f"decisions per succeeded task: {_show(dc['value'])}"
            f" ({dc['decisions']}/{dc['denominator']})",
            f"env_* block share: {_show(eb['value'])} ({eb['blocked']}/{eb['denominator']})",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", type=Path, required=True, help="read-only copy of argus.db")
    p.add_argument("--since", type=date.fromisoformat, required=True, help="UTC date")
    p.add_argument(
        "--until", type=date.fromisoformat, required=True, help="UTC date, inclusive"
    )
    p.add_argument("--merged", type=Path, help="JSON: task id or PR URL -> merged_at")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.until < args.since:
        p.error("--until is before --since")
    merged = json.loads(args.merged.read_text()) if args.merged else None
    db = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        result = report(db, args.since, args.until, merged)
    finally:
        db.close()
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
