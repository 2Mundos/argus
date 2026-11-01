"""Score one sample against a case's checks. Pure functions, no model."""

from evals.cases import CallPattern, Case
from evals.model import ToolCall
from evals.world import args_match


def matches(pattern: CallPattern, call: ToolCall) -> bool:
    return pattern.tool == call.name and args_match(pattern.args, call.args)


def grade(case: Case, calls: list[ToolCall], *, finished: bool) -> list[str]:
    """Return the failed checks; an empty list means the sample passed."""
    checks = case.checks
    failures: list[str] = []
    if not finished:
        failures.append(f"did not stop within {case.max_turns} turns")
    if checks.max_calls is not None and len(calls) > checks.max_calls:
        failures.append(f"{len(calls)} calls, more than {checks.max_calls}")
    failures += [
        f"forbidden call: {p.tool} {p.args or ''}".strip()
        for p in checks.forbid
        if any(matches(p, c) for c in calls)
    ]
    if checks.in_order:
        remaining = iter(calls)
        for pattern in checks.expect:
            if not any(matches(pattern, c) for c in remaining):
                failures.append(
                    f"missing or out of order: {pattern.tool} {pattern.args or ''}".strip()
                )
                break
    else:
        failures += [
            f"missing call: {p.tool} {p.args or ''}".strip()
            for p in checks.expect
            if not any(matches(p, c) for c in calls)
        ]
    return failures


def case_failed(failed_samples: int, samples: int) -> bool:
    """A case fails when half or more of its samples fail."""
    return failed_samples * 2 >= samples
