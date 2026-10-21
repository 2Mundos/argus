"""TaskCreate / TaskChanges size caps (security-hardening spec)."""

import pytest
from argus_core.task_edit import TaskChanges
from argus_core.tasks import INPUT_MAX_CHARS, SPEC_MAX_LENGTH, TaskCreate
from pydantic import ValidationError


def test_oversized_spec_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"spec"):
        TaskCreate(project="global", title="t", spec="x" * (SPEC_MAX_LENGTH + 1))


def test_oversized_input_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"input"):
        TaskCreate(
            project="global",
            title="t",
            input={"blob": "y" * (INPUT_MAX_CHARS + 1)},
        )


def test_oversized_spec_edit_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"spec"):
        TaskChanges(spec="z" * (SPEC_MAX_LENGTH + 1))


def test_within_caps_is_accepted() -> None:
    TaskCreate(project="global", title="t", spec="ok", input={"a": 1})
    TaskChanges(spec="ok")
