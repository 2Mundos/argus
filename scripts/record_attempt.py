#!/usr/bin/env python3
"""Record one task attempt from an Argus database as a replay fixture.

    ARGUS_DB=copy-of-argus.db scripts/record_attempt.py <task_id> <attempt> [-o out.json]

Reads the attempt's audit events, live log, progress reports, model reports and final
output, redacts secrets and absolute paths, and writes timestamps as offsets from the
first step. Review the fixture before committing it under
apps/api/tests/fixtures/replay/. The workspace diff is not recorded (v1).

Point it at a copy of the database: it opens the file read-only, never the live one.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api/src"))

from argus_api.replay import fixture

_EXECUTION = [
    "key",
    "layer",
    "state",
    "runtime",
    "engine",
    "executor",
    "provider",
    "requested_model",
    "configured_model",
    "observed_model",
    "evidence",
    "policy_revision",
    "delegation_id",
    "run_key",
    "resumed_from",
    "error_code",
]
_SELECTION = [
    "key",
    "layer",
    "kind",
    "reason_code",
    "explanation",
    "from_model",
    "to_model",
    "from_provider",
    "to_provider",
    "from_executor",
    "to_executor",
    "execution_key",
    "outcome",
]
_USAGE = ("input_tokens", "output_tokens", "cost_usd")


def _pick(row: sqlite3.Row, columns: list[str]) -> dict[str, Any]:
    return {c: row[c] for c in columns if row[c] is not None}


def _window(db: sqlite3.Connection, task_id: str, attempt: int) -> tuple[str, str]:
    """[claimed_at, next claim) of the attempt; `attempt` restarts after a retry, so
    the newest claim of that number wins and the next row ends it."""
    rows = db.execute(
        "SELECT attempt, claimed_at FROM task_attempts WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    for i in reversed(range(len(rows))):
        if rows[i]["attempt"] == attempt:
            end = rows[i + 1]["claimed_at"] if i + 1 < len(rows) else "9999"
            return rows[i]["claimed_at"], end
    raise SystemExit(f"task {task_id} has no recorded attempt {attempt}")


def record(db: sqlite3.Connection, task_id: str, attempt: int) -> dict[str, Any]:
    """The fixture for one attempt of a task."""
    db.row_factory = sqlite3.Row
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise SystemExit(f"task not found: {task_id}")
    start, end = _window(db, task_id, attempt)
    entries: list[tuple[str, dict[str, Any]]] = []

    entries += [
        (r["at"], {"type": "log", "source": r["source"], "line": r["line"]})
        for r in db.execute(
            "SELECT source, at, line FROM task_log WHERE task_id = ? AND attempt = ?"
            " ORDER BY seq",
            (task_id, attempt),
        )
    ]

    for r in db.execute(
        "SELECT at, kind, data FROM events WHERE subject_type = 'task'"
        " AND subject_id = ? AND at >= ? AND at < ? ORDER BY id",
        (task_id, start, end),
    ):
        data = json.loads(r["data"])
        if r["kind"] == "task.progress" and data.get("attempt") == attempt:
            step: dict[str, Any] = {
                "type": "progress",
                "message": data["message"],
                "percent": data.get("percent"),
            }
            if "kind" in data:
                step |= {"kind": data["kind"], "data": data.get("data")}
            entries.append((r["at"], step))
        else:  # audit rows are kept to read the story; the API writes them itself
            entries.append(
                (r["at"], {"type": "event", "kind": r["kind"], "data": data})
            )

    models: dict[str, dict[str, Any]] = {}
    for r in db.execute(
        "SELECT * FROM model_executions WHERE task_id = ? AND attempt = ?"
        " ORDER BY recorded_at",
        (task_id, attempt),
    ):
        models.setdefault(r["recorded_at"], {"executions": [], "selections": []})[
            "executions"
        ].append(_pick(r, _EXECUTION))
    for r in db.execute(
        "SELECT * FROM model_selections WHERE task_id = ? AND attempt = ? ORDER BY at",
        (task_id, attempt),
    ):
        selection = _pick(r, _SELECTION) | {
            "alternatives": json.loads(r["alternatives"])
        }
        models.setdefault(r["at"], {"executions": [], "selections": []})[
            "selections"
        ].append(selection)
    entries += [(at, {"type": "models", **body}) for at, body in models.items()]

    if not entries:
        raise SystemExit(f"attempt {attempt} of {task_id} recorded nothing")
    return fixture.build(entries, _final(task, attempt))


def _final(task: sqlite3.Row, attempt: int) -> dict[str, Any]:
    if task["attempt"] != attempt:
        raise SystemExit(
            "only the task's latest attempt keeps its final output;"
            f" {task['id']} is at attempt {task['attempt']}"
        )
    if task["result"] is not None:
        result = json.loads(task["result"])
        usage = result.get("usage") or {}
        return {
            "outcome": "complete",
            # Artifact ids and checklist items belong to the recorded task.
            "result": {
                "summary": result["summary"],
                "data": result.get("data"),
                **{k: usage[k] for k in _USAGE if usage.get(k) is not None},
            },
        }
    if task["error"]:
        return {"outcome": "fail", "error": task["error"]}
    raise SystemExit(f"{task['id']} has no final output (result or error)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("task_id")
    parser.add_argument("attempt", type=int)
    parser.add_argument("--db", default=os.environ.get("ARGUS_DB"), help="or $ARGUS_DB")
    parser.add_argument("-o", "--output", type=Path, help="default: stdout")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("give the database copy with --db or ARGUS_DB")
    db = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    try:
        out = json.dumps(record(db, args.task_id, args.attempt), indent=2) + "\n"
    finally:
        db.close()
    if args.output:
        args.output.write_text(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
