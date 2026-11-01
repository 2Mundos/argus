"""Task difficulty and its conservative default (docs/specs/task-difficulty.spec.md)."""

import re
from enum import StrEnum
from typing import Any


class Difficulty(StrEnum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    STANDARD = "standard"
    HARD = "hard"


# Ascending; an escalation moves one step right.
LEVELS = tuple(Difficulty)

# Delegate CLIs a difficulty executor chain may name (plus empty / `none` = none).
DIFFICULTY_EXECUTOR_CLIS = frozenset({"claude", "codex", "opencode", "agent"})
NONE_EXECUTOR = "none"


def parse_difficulty_executors(raw: object) -> dict[str, tuple[str, ...]]:
    """Validate a difficulty → executor-chain map. Empty list or sole `none` means
    no delegated executor. Raises ValueError with a fixed message on bad shape."""
    if not isinstance(raw, dict):
        raise ValueError(
            "difficulty.executors: a JSON object of difficulty -> list of executors"
        )
    out: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if key not in set(Difficulty):
            raise ValueError(
                f"difficulty.executors: unknown difficulty {key!r} "
                f"(want {', '.join(d.value for d in Difficulty)})"
            )
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ValueError(
                f"difficulty.executors.{key}: must be a list of executor names"
            )
        if value == [NONE_EXECUTOR] or value == []:
            out[key] = ()
            continue
        if NONE_EXECUTOR in value:
            raise ValueError(
                f"difficulty.executors.{key}: {NONE_EXECUTOR!r} must be alone "
                "(or use an empty list)"
            )
        if not set(value) <= DIFFICULTY_EXECUTOR_CLIS:
            raise ValueError(
                f"difficulty.executors.{key}: list of "
                f"{', '.join(sorted(DIFFICULTY_EXECUTOR_CLIS))}, empty, or "
                f"[{NONE_EXECUTOR!r}]"
            )
        out[key] = tuple(value)
    return out


def chain_for(
    difficulty: Difficulty | str,
    overrides: dict[str, tuple[str, ...]] | None,
    defaults: dict[Difficulty, tuple[str, ...]],
) -> tuple[str, ...]:
    """Effective executor chain for a level: override if present, else default."""
    key = difficulty if isinstance(difficulty, str) else difficulty.value
    if overrides is not None and key in overrides:
        return overrides[key]
    level = difficulty if isinstance(difficulty, Difficulty) else Difficulty(difficulty)
    return defaults[level]


def as_jsonable(chains: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """Settings / claim payload form (lists; empty = no executor)."""
    return {k: list(v) for k, v in chains.items()}


def merge_difficulty_executor_maps(
    *layers: dict[str, Any] | None,
) -> dict[str, tuple[str, ...]]:
    """Later layers win per difficulty key. Each layer is validated."""
    merged: dict[str, tuple[str, ...]] = {}
    for layer in layers:
        if not layer:
            continue
        merged.update(parse_difficulty_executors(layer))
    return merged


def escalated(difficulty: Difficulty) -> Difficulty:
    """One level harder; `hard` stays `hard`."""
    return LEVELS[min(LEVELS.index(difficulty) + 1, len(LEVELS) - 1)]


class DifficultySource(StrEnum):
    DECLARED = "declared"  # the creator or an editor chose it
    INFERRED = "inferred"  # a suggested classification (`classify`, the orchestrator)


MAX_TRIVIAL_SPEC = 1500

_COPY = re.compile(
    r"\b(copy ?(change|edit|tweak)?|typos?|wording|translations?|translate|"
    r"docs? only|readme|microcopy|label text)\b",
    re.IGNORECASE,
)
_RISK = re.compile(
    r"\b(migrations?|schemas?|apis?|endpoints?|security|auth\w*|passwords?|"
    r"credentials?|secrets?|tokens?|tests?|refactor\w*|bugs?|deploy\w*|"
    r"database|sql|performance|concurrency|permissions?)\b",
    re.IGNORECASE,
)


# Hard signals, by kind: `hard` needs two different kinds, never one alone (Opus is the
# expensive tier, so a false `hard` costs more than a missed one).
_HARD = {
    "architecture": re.compile(
        r"\b(architecture|redesign|re-?architect\w*)\b", re.IGNORECASE
    ),
    "data_migration": re.compile(
        r"\b(data migration|backfill|schema migration)\b",
        re.IGNORECASE,
    ),
    "security": re.compile(
        r"\b(security|vulnerabilit\w*|threat model)\b",
        re.IGNORECASE,
    ),
    "multi_service": re.compile(
        r"\b(multi-?service|cross-?service|distributed)\b",
        re.IGNORECASE,
    ),
}


def classify(title: str, spec: str = "") -> Difficulty:
    """`hard` only for two or more distinct hard signals; `trivial` only for clear
    copy/text/docs signals with no code-risk signal and a short spec; everything else
    is `standard`; never `simple`. Used only when the creator sends no difficulty."""
    text = f"{title}\n{spec}"
    if sum(1 for rx in _HARD.values() if rx.search(text)) >= 2:
        return Difficulty.HARD
    if len(spec) > MAX_TRIVIAL_SPEC or _RISK.search(text) or not _COPY.search(text):
        return Difficulty.STANDARD
    return Difficulty.TRIVIAL
