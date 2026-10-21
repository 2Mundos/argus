import itertools

import pytest
from argus_core.tasks import (
    Action,
    InvalidTransitionError,
    Review,
    TaskState,
    TransitionContext,
    transition,
)

S, A = TaskState, Action
CTX = TransitionContext()

# Every legal (state, action) with the default context. Anything absent must raise.
LEGAL: dict[tuple[TaskState, Action], TaskState] = {
    (S.AWAITING_APPROVAL, A.APPROVE): S.READY,
    (S.PENDING, A.DEPS_SATISFIED): S.READY,
    (S.READY, A.DEPS_ADDED): S.PENDING,
    (S.READY, A.CLAIM): S.CLAIMED,
    (S.CLAIMED, A.COMPLETE): S.SUCCEEDED,
    (S.CLAIMED, A.FAIL): S.READY,
    (S.CLAIMED, A.EXPIRE): S.READY,
    (S.CLAIMED, A.BLOCK): S.BLOCKED,
    (S.READY, A.BLOCK): S.BLOCKED,
    (S.CLAIMED, A.REQUEUE): S.READY,
    (S.CLAIMED, A.RELEASE): S.READY,
    (S.BLOCKED, A.UNBLOCK): S.READY,
    (S.IN_REVIEW, A.ACCEPT): S.SUCCEEDED,
    (S.IN_REVIEW, A.REJECT): S.READY,
    **{
        (s, A.CANCEL): S.CANCELLED
        for s in S
        if s not in {S.SUCCEEDED, S.FAILED, S.CANCELLED}
    },
}


@pytest.mark.parametrize(("state", "action"), list(itertools.product(S, A)))
def test_every_state_action_pair(state: TaskState, action: Action) -> None:
    expected = LEGAL.get((state, action))
    if expected is None:
        with pytest.raises(InvalidTransitionError):
            transition(state, action, CTX)
    else:
        assert transition(state, action, CTX) is expected


def test_approve_waits_for_deps() -> None:
    ctx = TransitionContext(deps_done=False)
    assert transition(S.AWAITING_APPROVAL, A.APPROVE, ctx) is S.PENDING


@pytest.mark.parametrize("review", [Review.HUMAN, Review.ORCHESTRATOR])
def test_complete_with_review_goes_to_review(review: Review) -> None:
    ctx = TransitionContext(review=review)
    assert transition(S.CLAIMED, A.COMPLETE, ctx) is S.IN_REVIEW


@pytest.mark.parametrize("action", [A.FAIL, A.EXPIRE])
@pytest.mark.parametrize(
    ("failures", "expected"), [(0, S.READY), (1, S.READY), (2, S.FAILED)]
)
def test_failures_count_toward_max_attempts(
    action: Action, failures: int, expected: TaskState
) -> None:
    ctx = TransitionContext(failures=failures, max_attempts=3)
    assert transition(S.CLAIMED, action, ctx) is expected


def test_single_attempt_task_fails_immediately() -> None:
    ctx = TransitionContext(max_attempts=1)
    assert transition(S.CLAIMED, A.FAIL, ctx) is S.FAILED
