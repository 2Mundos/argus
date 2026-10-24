"""Task difficulty and its conservative default (docs/specs/task-difficulty.spec.md)."""

import re
from enum import StrEnum


class Difficulty(StrEnum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    STANDARD = "standard"
    HARD = "hard"


# Ascending; an escalation moves one step right.
LEVELS = tuple(Difficulty)


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
