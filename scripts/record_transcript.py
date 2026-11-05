#!/usr/bin/env python3
"""Copy one attempt's raw engine events into a transcript case directory.

    ARGUS_DB=copy-of-argus.db scripts/record_transcript.py <task_id> <attempt> \
        <engine> <case> [--root DIR]

Reads the attempt's delegate log (the engine's stream-json output, one event per row)
and writes <root>/<engine>/<case>/events.jsonl. Long strings, which are tool output,
are cut to a cap and the event structure is kept; the whole file stays under 200 KB.
Redaction beyond what the API already stored, expected.json and source.md are not
written yet. See docs/drafts/2026-11-04.engine-transcript-replay.draft.md.

Point it at a copy of the database: it opens the file read-only, never the live one.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = (
    Path(__file__).resolve().parents[1] / "integrations/nanoclaw/fixtures/transcripts"
)
MAX_BYTES = 200 * 1024
STRING_CAPS = (2000, 500, 120)


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return f"{text[:cap]}… [{len(text) - cap} chars trimmed]"


def trim(value: Any, cap: int) -> Any:
    """`value` with every string longer than `cap` cut; keys, nesting and order stay."""
    if isinstance(value, str):
        return _cut(value, cap)
    if isinstance(value, list):
        return [trim(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: trim(v, cap) for k, v in value.items()}
    return value


def render(lines: list[str]) -> str:
    """events.jsonl text for the log lines, trimmed with the largest cap that fits."""
    for cap in STRING_CAPS:
        rows = []
        for line in lines:
            try:
                rows.append(json.dumps(trim(json.loads(line), cap), ensure_ascii=False))
            except ValueError:  # not JSON (stderr or plain text): keep it as a line
                rows.append(_cut(line, cap).replace("\n", " "))
        text = "\n".join(rows) + "\n"
        if len(text.encode()) <= MAX_BYTES:
            return text
    raise SystemExit(
        f"events stay above {MAX_BYTES} bytes with a {STRING_CAPS[-1]} cap"
    )


def record(db: sqlite3.Connection, task_id: str, attempt: int) -> list[str]:
    lines = [
        r[0]
        for r in db.execute(
            "SELECT line FROM task_log WHERE task_id = ? AND attempt = ?"
            " AND source = 'delegate' ORDER BY seq",
            (task_id, attempt),
        )
    ]
    if not lines:
        raise SystemExit(f"attempt {attempt} of {task_id} has no delegate log lines")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("task_id")
    parser.add_argument("attempt", type=int)
    parser.add_argument("engine")
    parser.add_argument("case")
    parser.add_argument("--db", default=os.environ.get("ARGUS_DB"), help="or $ARGUS_DB")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("give the database copy with --db or ARGUS_DB")
    for part in (args.engine, args.case):
        if not part or part in (".", "..") or "/" in part or "\\" in part:
            parser.error(f"not a plain directory name: {part!r}")
    db = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    try:
        text = render(record(db, args.task_id, args.attempt))
    finally:
        db.close()
    case = args.root / args.engine / args.case
    case.mkdir(parents=True, exist_ok=True)
    (case / "events.jsonl").write_text(text)
    print(case / "events.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
