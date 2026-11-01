import pytest

from evals.cases import CallPattern, Case, Checks
from evals.grade import case_failed, grade
from evals.tests.fakes import call


def make(**checks) -> Case:
    return Case("c", "task:x", "do it", [], Checks(**checks), max_turns=3)


def test_expected_args_and_wildcard():
    case = make(
        expect=[CallPattern("answer", {"id": "d1", "request_id": "*"})],
    )
    assert grade(case, [call("answer", id="d1", request_id="r")], finished=True) == []
    assert grade(case, [call("answer", id="d1")], finished=True) == [
        "missing call: answer {'id': 'd1', 'request_id': '*'}"
    ]
    assert grade(case, [call("answer", id="d2", request_id="r")], finished=True)


def test_forbidden_call_with_and_without_args():
    whole = make(forbid=[CallPattern("cancel")])
    partial = make(forbid=[CallPattern("answer", {"choice": "accept"})])
    assert grade(whole, [call("cancel")], finished=True)
    assert grade(partial, [call("answer", choice="reject")], finished=True) == []
    assert grade(partial, [call("answer", choice="accept")], finished=True)


def test_order_is_only_enforced_when_asked():
    expect = [CallPattern("claim"), CallPattern("complete")]
    calls = [call("complete"), call("claim")]
    assert grade(make(expect=expect), calls, finished=True) == []
    ordered = grade(make(expect=expect, in_order=True), calls, finished=True)
    assert ordered == ["missing or out of order: complete"]


def test_ordered_calls_may_be_interleaved_with_others():
    case = make(expect=[CallPattern("a"), CallPattern("b")], in_order=True)
    assert grade(case, [call("a"), call("x"), call("b")], finished=True) == []


def test_stop_and_call_budget():
    case = make(max_calls=1)
    failures = grade(case, [call("a"), call("b")], finished=False)
    assert failures == ["did not stop within 3 turns", "2 calls, more than 1"]


@pytest.mark.parametrize(
    ("failed", "samples", "expected"),
    [
        (0, 5, False),
        (2, 5, False),
        (3, 5, True),
        (2, 4, True),
        (1, 2, True),
        (1, 3, False),
    ],
)
def test_case_fails_at_half_or_more(failed, samples, expected):
    assert case_failed(failed, samples) is expected
