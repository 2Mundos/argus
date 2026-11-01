"""Cases are data: a prompt, a recorded world and the checks that score a run."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CallPattern:
    """A tool call: the tool, and args it must include ("*" matches any value)."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Checks:
    expect: list[CallPattern] = field(default_factory=list)
    forbid: list[CallPattern] = field(default_factory=list)
    in_order: bool = False
    max_calls: int | None = None
    rubric: str | None = None


@dataclass(frozen=True)
class Case:
    id: str
    source: str
    prompt: str
    world: list[dict[str, Any]]
    checks: Checks
    control: bool = False
    max_turns: int = 12


def parse_case(data: dict[str, Any]) -> Case:
    checks = data["checks"]
    return Case(
        id=data["id"],
        source=data["source"],
        prompt=data["prompt"],
        world=data["world"],
        control=data.get("control", False),
        max_turns=data.get("max_turns", 12),
        checks=Checks(
            expect=[CallPattern(**p) for p in checks.get("expect", [])],
            forbid=[CallPattern(**p) for p in checks.get("forbid", [])],
            in_order=checks.get("in_order", False),
            max_calls=checks.get("max_calls"),
            rubric=checks.get("rubric"),
        ),
    )


def load_cases(directory: Path) -> list[Case]:
    return [
        parse_case(json.loads(f.read_text())) for f in sorted(directory.glob("*.json"))
    ]
