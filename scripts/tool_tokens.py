#!/usr/bin/env python3
"""First-turn token cost of the MCP tool text, per role (docs/drafts/2026-11-04.tool-description-rules.draft.md).

    scripts/tool_tokens.py

Builds the orchestrator and worker prompt and tool list through evals/source.py, the
import path the evals use, and prints each role's total plus a per-tool breakdown.

Counting is a local approximation, with no network and no dependency: one token per
run of letters or digits of up to four characters (longer runs add one per four
characters, rounded up) and one per punctuation mark. It is deterministic and tracks
real tokenizers closely enough to compare a change before and after; it is not a
provider-exact count.
"""

import json
import math
import re
import sys
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


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"{report['role']}: {report['total']} tokens "
        f"(prompt {report['prompt']}, {len(report['tools'])} tools "
        f"{sum(n for _, n in report['tools'])})"
    ]
    lines += [f"  {n:6d}  {name}" for name, n in report["tools"]]
    return "\n".join(lines)


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `evals`
    from argus_core.auth import Scope

    from evals.source import orchestrator_prompt, tools_for, worker_prompt

    roles = (
        ("orchestrator", Scope.ORCHESTRATOR, orchestrator_prompt()),
        ("worker", Scope.WORKER, worker_prompt()),
    )
    print(
        "\n\n".join(
            format_report(build_report(r, tools_for(s), p)) for r, s, p in roles
        )
    )


if __name__ == "__main__":
    main()
