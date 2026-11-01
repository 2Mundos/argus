"""The replay fixture: one recorded attempt as a time-ordered list of steps.

Only the event stream is recorded (log lines, progress reports, model reports, audit
events and the final output). The workspace diff is not part of a v1 fixture, so task
changes and delivery are not replayed. Every `t` is seconds from the first step.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from argus_core.redact import redact

VERSION = 1
STEP_TYPES = ("log", "progress", "models", "event")

# An absolute path under a common root, up to the next space or quote. Best effort.
_PATH = re.compile(
    r"(?<![\w.:/-])(?:/(?:home|Users|root|workspace|tmp|var|opt|srv|mnt|data|usr|etc)"
    r"|[A-Za-z]:\\)[^\s\"'`,;)\]]*"
)
PATH_PLACEHOLDER = "<path>"


def clean_text(text: str) -> str:
    """`text` without credentials or absolute paths."""
    return _PATH.sub(PATH_PLACEHOLDER, redact(text))


def clean(value: Any) -> Any:
    """`value` with every string in it cleaned (dict keys are kept)."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def offsets(times: list[str]) -> list[float]:
    """Seconds from the earliest of `times` (ISO-8601 text), rounded to ms."""
    if not times:
        return []
    parsed = [datetime.fromisoformat(t) for t in times]
    base = min(parsed)
    return [round((p - base).total_seconds(), 3) for p in parsed]


def build(
    entries: list[tuple[str, dict[str, Any]]], final: dict[str, Any]
) -> dict[str, Any]:
    """A fixture from `(timestamp, step)` pairs and the final output. Steps are
    stable-sorted by time, cleaned, and stamped with their offset."""
    ordered = sorted(entries, key=lambda e: datetime.fromisoformat(e[0]))
    rel = offsets([at for at, _ in ordered])
    steps = [{"t": t, **clean(step)} for t, (_, step) in zip(rel, ordered, strict=True)]
    return {"version": VERSION, "steps": steps, "final": clean(final)}


def load(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text())
    if fixture.get("version") != VERSION:
        raise ValueError(f"{path}: unsupported fixture version")
    last = 0.0
    for step in fixture["steps"]:
        if step.get("type") not in STEP_TYPES:
            raise ValueError(f"{path}: unknown step type {step.get('type')!r}")
        if step["t"] < last:
            raise ValueError(f"{path}: steps must be ordered by t")
        last = step["t"]
    if fixture["final"].get("outcome") not in ("complete", "fail"):
        raise ValueError(f"{path}: final.outcome must be complete or fail")
    return fixture
