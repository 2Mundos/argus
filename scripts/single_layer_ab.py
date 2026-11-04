#!/usr/bin/env python3
"""Single-layer experiment (P7): assign tasks to arms, export attempts, compare arms.

    scripts/single_layer_ab.py assign --candidates tasks.json --experiment p7-1 --seed S
    scripts/single_layer_ab.py export --db copy-of-argus.db --assignment a.json
    scripts/single_layer_ab.py analyze --attempts attempts.jsonl --assignment a.json
        [--merged merged.json]

`assign` writes the file the NanoClaw host reads (ARGUS_SINGLE_LAYER_ASSIGNMENT).
`export` reads a copy of the database read-only and writes one JSON line per attempt.
`analyze` prints per-arm figures. Unknown usage is excluded and counted, never 0; a
task with no pull request, or none merged, is censored, never given a time. See
docs/specs/single-layer-experiment.spec.md. Output computed from fixtures or mocked
runs is not an experiment result.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ARMS = ("single_layer", "delegated")
EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MARKER = re.compile(
    r"^single-layer experiment (?P<experiment>\S+): arm=(?P<arm>single_layer|delegated)"
    r"(?: assigned=(?P<assigned>\S+) fallback=(?P<fallback>\S+))?$"
)
FINISHED = {"succeeded", "failed", "cancelled"}


# ---- assignment (pure) ----


def rank(seed: str, experiment: str, key: str) -> str:
    """A stable pseudo-random order: the same inputs give the same rank anywhere."""
    return hashlib.sha256(f"{seed}\0{experiment}\0{key}".encode()).hexdigest()


def stratum(task: dict[str, Any]) -> str:
    return str(task.get("stratum") or task.get("difficulty") or "standard")


def select(
    candidates: list[dict[str, Any]], size: int, seed: str, experiment: str
) -> list[dict[str, Any]]:
    """`size` candidates, each stratum in proportion to its share (largest remainder),
    and within a stratum the lowest ranks."""
    if size >= len(candidates):
        return list(candidates)
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in sorted(candidates, key=lambda t: rank(seed, experiment, t["id"])):
        groups.setdefault(stratum(t), []).append(t)
    exact = {s: size * len(g) / len(candidates) for s, g in groups.items()}
    quota = {s: int(q) for s, q in exact.items()}
    for s in sorted(exact, key=lambda s: (quota[s] - exact[s], s))[
        : size - sum(quota.values())
    ]:
        quota[s] += 1
    return [t for s, g in sorted(groups.items()) for t in g[: quota[s]]]


def assign(tasks: list[dict[str, Any]], seed: str, experiment: str) -> dict[str, str]:
    """Alternate arms in rank order within each stratum, so every stratum is balanced
    to within one task; the stratum's own rank picks which arm takes the odd one."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in sorted(tasks, key=lambda t: rank(seed, experiment, t["id"])):
        groups.setdefault(stratum(t), []).append(t)
    arms: dict[str, str] = {}
    for s, group in sorted(groups.items()):
        first = int(rank(seed, experiment, f"stratum:{s}"), 16) % 2
        for i, t in enumerate(group):
            arms[t["id"]] = ARMS[(first + i) % 2]
    return arms


def assignment_doc(
    candidates: list[dict[str, Any]], size: int, seed: str, experiment: str
) -> dict[str, Any]:
    if not EXPERIMENT_ID.match(experiment):
        raise ValueError(f"invalid experiment id {experiment!r}")
    if len({t["id"] for t in candidates}) != len(candidates):
        raise ValueError("candidate task ids are not unique")
    chosen = select(candidates, size, seed, experiment)
    return {
        "experiment": experiment,
        "engine": "claude",
        "seed": seed,
        "arms": assign(chosen, seed, experiment),
        "strata": {t["id"]: stratum(t) for t in chosen},
    }


# ---- export (read-only database copy) ----


def _usage(rows: list[sqlite3.Row]) -> tuple[int | None, float | None]:
    """Tokens and cost of one attempt; None when any part of it is unknown."""
    if not rows or any(r["measurement"] != "measured" for r in rows):
        return None, None
    inputs = [r["input_tokens"] for r in rows]
    outputs = [r["output_tokens"] for r in rows]
    tokens = (
        None if None in inputs or None in outputs else sum(inputs) + sum(outputs)  # type: ignore[arg-type]
    )
    costs = [r["cost_usd"] for r in rows]
    return tokens, None if None in costs else sum(costs)  # type: ignore[arg-type]


def export(db: sqlite3.Connection, assignment: dict[str, Any]) -> list[dict[str, Any]]:
    """One record per claim of an assigned task. A claim's window runs to the task's
    next claim (`attempt` restarts after a retry, so the number alone is ambiguous)."""
    db.row_factory = sqlite3.Row
    out = []
    for task_id in sorted(assignment["arms"]):
        task = db.execute(
            "SELECT state, created_at, difficulty FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            continue
        claims = db.execute(
            "SELECT attempt, claimed_at FROM task_attempts WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        events = db.execute(
            "SELECT at, kind, data FROM events WHERE subject_type = 'task'"
            " AND subject_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        for i, c in enumerate(claims):
            start = c["claimed_at"]
            end = claims[i + 1]["claimed_at"] if i + 1 < len(claims) else "9999"
            inside = [
                (e["at"], e["kind"], json.loads(e["data"]))
                for e in events
                if start <= e["at"] < end
            ]
            ended = next((k for _, k, d in inside if d.get("from") == "claimed"), None)
            pr = next(
                (
                    (at, (d.get("data") or {}).get("url"))
                    for at, k, d in inside
                    if k == "task.progress" and d.get("kind") == "pr_opened"
                ),
                (None, None),
            )
            marker = None
            for (explanation,) in db.execute(
                "SELECT explanation FROM model_selections WHERE task_id = ?"
                " AND layer = 'harness' AND at >= ? AND at < ? ORDER BY at",
                (task_id, start, end),
            ):
                m = MARKER.match(explanation or "")
                if m and m["experiment"] == assignment["experiment"]:
                    marker = m
                    break
            tokens, cost = _usage(
                db.execute(
                    "SELECT input_tokens, output_tokens, cost_usd, measurement"
                    " FROM account_usage WHERE task_id = ? AND attempt = ?"
                    " AND at >= ? AND at < ?",
                    (task_id, c["attempt"], start, end),
                ).fetchall()
            )
            out.append(
                {
                    "task_id": task_id,
                    "attempt": c["attempt"],
                    "claim": i + 1,
                    "task_state": task["state"],
                    "difficulty": task["difficulty"],
                    "created_at": task["created_at"],
                    "claimed_at": start,
                    "ended": ended,
                    "arm": marker["arm"] if marker else None,
                    "fallback": marker["fallback"] if marker else None,
                    "tokens": tokens,
                    "cost_usd": cost,
                    "pr_opened_at": pr[0],
                    "pr_url": pr[1],
                }
            )
    return out


# ---- analysis (pure) ----


def _t(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _hours(a: str, b: str) -> float:
    return round((_t(b) - _t(a)).total_seconds() / 3600, 3)


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def _summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3) if values else None,
        "mean": round(statistics.fmean(values), 3) if values else None,
    }


def _times(observed: list[float], censored: int) -> dict[str, Any]:
    """Observed durations and the right-censored count. The median of the observed
    durations alone is biased low when anything is censored, so it is flagged."""
    return {
        **_summary(observed),
        "censored": censored,
        "median_biased_low": censored > 0,
    }


def _per_success(
    by_task: dict[str, list[dict[str, Any]]], succeeded: list[str], field: str
) -> tuple[list[float], int]:
    """A succeeded task's total over all its attempts; unknown if any is unknown."""
    known, unknown = [], 0
    for t in succeeded:
        values = [a[field] for a in by_task[t]]
        if None in values:
            unknown += 1
        else:
            known.append(float(sum(values)))
    return known, unknown


def analyze(
    attempts: list[dict[str, Any]],
    assignment: dict[str, Any],
    merged: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Per assigned arm (intention to treat): a fallback attempt stays in its
    assigned arm and is counted. `merged` maps a pull request URL to its merge time,
    taken from the forge; without it no merge time exists."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for a in attempts:
        if a["task_id"] in assignment["arms"]:
            by_task.setdefault(a["task_id"], []).append(a)
    strata = assignment.get("strata", {})
    arms: dict[str, Any] = {}
    for arm in ARMS:
        ids = sorted(t for t, x in assignment["arms"].items() if x == arm)
        runs = [a for t in ids for a in by_task.get(t, [])]
        ended = [a for a in runs if a["ended"]]
        state = {t: by_task[t][0]["task_state"] for t in ids if t in by_task}
        finished = [t for t in ids if state.get(t) in FINISHED]
        succeeded = [t for t in finished if state[t] == "succeeded"]
        tokens, tokens_unknown = _per_success(by_task, succeeded, "tokens")
        cost, cost_unknown = _per_success(by_task, succeeded, "cost_usd")
        to_pr_created, to_pr_claim, to_merge, no_pr, unmerged = [], [], [], 0, 0
        for t in ids:
            runs_t = sorted(by_task.get(t, []), key=lambda a: a["claimed_at"])
            prs = sorted(
                (a["pr_opened_at"], a["pr_url"]) for a in runs_t if a["pr_opened_at"]
            )
            if not prs:
                no_pr += 1
                continue
            at = prs[0][0]
            to_pr_created.append(_hours(runs_t[0]["created_at"], at))
            to_pr_claim.append(_hours(runs_t[0]["claimed_at"], at))
            merged_at = next(
                (merged[u] for _, u in prs if merged and u in merged), None
            )
            if merged_at:
                to_merge.append(_hours(runs_t[0]["created_at"], merged_at))
            else:
                unmerged += 1
        arms[arm] = {
            "tasks_assigned": len(ids),
            "tasks_claimed": sum(1 for t in ids if t in by_task),
            "tasks_finished": len(finished),
            "tasks_succeeded": len(succeeded),
            "task_success_rate": _rate(len(succeeded), len(finished)),
            "attempts": len(runs),
            "attempts_ended": len(ended),
            "attempts_completed": sum(
                1 for a in ended if a["ended"] == "task.complete"
            ),
            "attempt_success_rate": _rate(
                sum(1 for a in ended if a["ended"] == "task.complete"), len(ended)
            ),
            "attempts_per_claimed_task": _rate(
                len(runs), sum(1 for t in ids if t in by_task)
            ),
            "attempts_unmarked": sum(1 for a in runs if a["arm"] is None),
            "attempts_off_arm": sum(1 for a in runs if a["arm"] not in (None, arm)),
            "fallbacks": dict(Counter(a["fallback"] for a in runs if a["fallback"])),
            "tokens_per_succeeded_task": {
                **_summary(tokens),
                "unknown": tokens_unknown,
            },
            "cost_usd_per_succeeded_task": {**_summary(cost), "unknown": cost_unknown},
            "hours_created_to_pr_opened": _times(to_pr_created, no_pr),
            "hours_first_claim_to_pr_opened": _times(to_pr_claim, no_pr),
            "hours_created_to_merged": _times(to_merge, no_pr + unmerged),
        }
    balance: dict[str, dict[str, int]] = {}
    for t, arm in assignment["arms"].items():
        row = balance.setdefault(strata.get(t, "unknown"), dict.fromkeys(ARMS, 0))
        row[arm] += 1
    return {
        "experiment": assignment["experiment"],
        "merge_data": merged is not None,
        "balance": dict(sorted(balance.items())),
        "arms": arms,
    }


# ---- CLI ----


def _write(out: str, path: Path | None) -> None:
    if path:
        path.write_text(out)
    else:
        sys.stdout.write(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("assign", help="select and assign tasks to arms")
    a.add_argument("--candidates", type=Path, required=True, help="JSON list of tasks")
    a.add_argument("--experiment", required=True)
    a.add_argument("--seed", required=True)
    a.add_argument("--size", type=int, default=20)
    e = sub.add_parser("export", help="attempts of assigned tasks from a database copy")
    e.add_argument("--db", type=Path, required=True)
    e.add_argument("--assignment", type=Path, required=True)
    n = sub.add_parser("analyze", help="per-arm figures from exported attempts")
    n.add_argument("--attempts", type=Path, required=True, help="JSON lines")
    n.add_argument("--assignment", type=Path, required=True)
    n.add_argument("--merged", type=Path, help="forge export: {pr_url: merged_at}")
    for p in (a, e, n):
        p.add_argument("-o", "--output", type=Path, help="default: stdout")
    args = parser.parse_args(argv)
    if args.command == "assign":
        doc = assignment_doc(
            json.loads(args.candidates.read_text()),
            args.size,
            args.seed,
            args.experiment,
        )
        _write(json.dumps(doc, indent=2) + "\n", args.output)
    elif args.command == "export":
        db = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
        try:
            rows = export(db, json.loads(args.assignment.read_text()))
        finally:
            db.close()
        _write("".join(json.dumps(r) + "\n" for r in rows), args.output)
    else:
        attempts = [
            json.loads(line)
            for line in args.attempts.read_text().splitlines()
            if line.strip()
        ]
        merged = json.loads(args.merged.read_text()) if args.merged else None
        report = analyze(attempts, json.loads(args.assignment.read_text()), merged)
        _write(json.dumps(report, indent=2) + "\n", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
