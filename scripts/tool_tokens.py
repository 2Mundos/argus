#!/usr/bin/env python3
"""First-turn token cost of the MCP tool text, per role (docs/drafts/2026-11-04.tool-description-rules.draft.md).

    scripts/tool_tokens.py [--base REF] [--json]

Builds the orchestrator and worker prompt and tool list through evals/source.py, the
import path the evals use, and prints each role's total plus a per-tool breakdown and
the difference from REF (default origin/main). REF is measured in a temporary detached
worktree, with this script and the REF checkout's own `argus_api` and `argus_core`.

Counting is a local approximation, with no network and no dependency: one token per
run of letters or digits of up to four characters (longer runs add one per four
characters, rounded up) and one per punctuation mark. It is deterministic and tracks
real tokenizers closely enough to compare a change before and after; it is not a
provider-exact count.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PIECE = re.compile(r"[A-Za-z0-9]+|[^\sA-Za-z0-9]")


def count_tokens(text: str) -> int:
    """Approximate token count of `text`."""
    return sum(
        math.ceil(len(piece) / 4) if piece[0].isalnum() else 1
        for piece in PIECE.findall(text)
    )


def tool_tokens(tool: Any) -> int:
    """Tokens of a tool as listed to the model: name, description and input schema JSON."""
    schema = json.dumps(tool.input_schema, separators=(",", ":"), sort_keys=True)
    return (
        count_tokens(tool.name) + count_tokens(tool.description) + count_tokens(schema)
    )


def build_report(role: str, tools: Sequence[Any], prompt: str) -> dict[str, Any]:
    """Totals and a per-tool breakdown, largest first (ties by name)."""
    per_tool = sorted(
        ((t.name, tool_tokens(t)) for t in tools), key=lambda item: (-item[1], item[0])
    )
    prompt_tokens = count_tokens(prompt)
    return {
        "role": role,
        "prompt": prompt_tokens,
        "tools": per_tool,
        "total": prompt_tokens + sum(n for _, n in per_tool),
    }


def format_diff(total: int, base_total: int, ref: str) -> str:
    """`+12 vs origin/main`: the signed difference of `total` from the base total."""
    return f"{total - base_total:+d} vs {ref}"


def format_report(
    report: dict[str, Any], base: dict[str, Any] | None = None, ref: str = ""
) -> str:
    diff = f", {format_diff(report['total'], base['total'], ref)}" if base else ""
    lines = [
        f"{report['role']}: {report['total']} tokens{diff} "
        f"(prompt {report['prompt']}, {len(report['tools'])} tools "
        f"{sum(n for _, n in report['tools'])})"
    ]
    lines += [f"  {n:6d}  {name}" for name, n in report["tools"]]
    return "\n".join(lines)


def current_reports(root: Path) -> list[dict[str, Any]]:
    sys.path.insert(0, str(root))  # for `evals`
    from argus_core.auth import Scope

    from evals.source import orchestrator_prompt, tools_for, worker_prompt

    roles = (
        ("orchestrator", Scope.ORCHESTRATOR, orchestrator_prompt()),
        ("worker", Scope.WORKER, worker_prompt()),
    )
    return [build_report(r, tools_for(s), p) for r, s, p in roles]


def base_reports(ref: str) -> list[dict[str, Any]]:
    """Reports for `ref`, built in a temporary detached worktree that is always removed."""
    subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        check=True,
        capture_output=True,
    )
    with tempfile.TemporaryDirectory(prefix="tool-tokens-") as tmp:
        tree = Path(tmp) / "base"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(tree), ref],
            check=True,
            capture_output=True,
        )
        try:
            env = {
                **os.environ,
                "TOOL_TOKENS_ROOT": str(tree),
                "PYTHONPATH": os.pathsep.join(
                    [str(tree / "apps/api/src"), str(tree / "packages/core/src")]
                ),
            }
            # This script, not the checkout's: REF may predate `--json`.
            out = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--json"],
                check=True,
                capture_output=True,
                text=True,
                cwd=tree,
                env=env,
            ).stdout
            return json.loads(out)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)], capture_output=True
            )
            subprocess.run(["git", "worktree", "prune"], capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", metavar="REF")
    parser.add_argument("--json", action="store_true", help="print the reports as JSON")
    args = parser.parse_args()

    # TOOL_TOKENS_ROOT: the checkout to measure (set for the base run).
    root = Path(
        os.environ.get("TOOL_TOKENS_ROOT") or Path(__file__).resolve().parent.parent
    )
    reports = current_reports(root)
    if args.json:
        print(json.dumps(reports))
        return
    try:
        base = {r["role"]: r for r in base_reports(args.base)}
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
        sys.exit(f"cannot measure {args.base}: {detail or e}")
    print("\n\n".join(format_report(r, base[r["role"]], args.base) for r in reports))


if __name__ == "__main__":
    main()
