#!/usr/bin/env python3
"""Count the stderr lines written by repeat_guard.py in one or more log files:
`repeat_guard: <tool_name> called <count> times in a row`. Prints a per-threshold
table and per-tool totals so an operator can see which tools loop. Standard
library only.

Usage: repeat_guard_counts.py [--json] PATH [PATH ...]
Exit 0 on success (even with no matches), 2 if a path cannot be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable

THRESHOLDS = (3, 5, 8)  # mirrors repeat_guard.THRESHOLDS
LINE = re.compile(r"^repeat_guard: (.+) called (\d+) times in a row$")


def count_lines(lines: Iterable[str]) -> dict:
    """Count hook lines: per-threshold, other counts, and per-tool threshold totals."""
    by_threshold = dict.fromkeys(THRESHOLDS, 0)
    other = 0
    tools: Counter[str] = Counter()
    for line in lines:
        match = LINE.match(line.strip())
        if not match:
            continue
        count = int(match.group(2))
        if count in by_threshold:
            by_threshold[count] += 1
            tools[match.group(1)] += 1
        else:
            other += 1
    return {
        "thresholds": by_threshold,
        "other": other,
        "total": sum(by_threshold.values()) + other,
        "tools": dict(sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def format_table(result: dict) -> str:
    rows = [(str(t), n) for t, n in result["thresholds"].items()]
    rows += [("other", result["other"]), ("total", result["total"])]
    out = ["count  lines"] + [f"{label:<6} {n}" for label, n in rows]
    if result["tools"]:
        out += ["", "tool  lines"] + [f"{t}  {n}" for t, n in result["tools"].items()]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", metavar="PATH")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)

    lines: list[str] = []
    for path in args.paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines.extend(f.read().splitlines())
        except OSError as exc:
            print(f"repeat_guard_counts: cannot read {path}: {exc}", file=sys.stderr)
            return 2
    result = count_lines(lines)
    print(json.dumps(result) if args.json else format_table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
