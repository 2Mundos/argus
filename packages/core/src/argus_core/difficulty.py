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
    r"\b(copy ?(change|edit|tweak)?|texto|typos?|erro de digita[cç][aã]o|wording|"
    r"reda[cç][aã]o|tradu[cç][aã]o|traduzir|translations?|translate|"
    r"docs? only|somente docs?|apenas docs?|readme|microcopy|r[oó]tulo|label text)\b",
    re.IGNORECASE,
)
_RISK = re.compile(
    r"\b(migrations?|schemas?|apis?|endpoints?|security|seguran[cç]a|auth\w*|autentica\w*|autoriza\w*|senhas?|passwords?|migra[cç][aã]o|migra[cç][oõ]es|esquema|"
    r"credentials?|credenciais|secrets?|tokens?|tests?|testes?|refactor\w*|"
    r"refatora\w*|bugs?|deploy\w*|database|banco de dados|sql|performance|"
    r"concurrency|permissions?|permiss[oõ]es)\b",
    re.IGNORECASE,
)


# Hard signals, by kind: `hard` needs two different kinds, never one alone (Opus is the
# expensive tier, so a false `hard` costs more than a missed one).
_HARD = {
    "architecture": re.compile(
        r"\b(architecture|arquitetura|redesign|re-?architect\w*)\b", re.IGNORECASE
    ),
    "data_migration": re.compile(
        r"\b(data migration|migra[cç][aã]o de dados|backfill|schema migration)\b",
        re.IGNORECASE,
    ),
    "security": re.compile(
        r"\b(security|seguran[cç]a|vulnerabilit\w*|vulnerabilidade\w*|threat model)\b",
        re.IGNORECASE,
    ),
    "multi_service": re.compile(
        r"\b(multi-?service|cross-?service|v[aá]rios servi[cç]os|"
        r"m[uú]ltiplos servi[cç]os|distributed|distribu[ií]d[oa])\b",
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
