from argus_core.task_edit import (
    CLEARABLE,
    EDITABLE,
    EDITABLE_BY_STATE,
    TaskChanges,
    editable_fields,
)
from argus_core.tasks import TaskState


def test_every_state_has_a_policy_within_the_editable_fields() -> None:
    assert set(EDITABLE_BY_STATE) == set(TaskState)
    for allowed in EDITABLE_BY_STATE.values():
        assert allowed <= set(EDITABLE)


def test_editable_fields_keep_the_canonical_order_and_archived_is_read_only() -> None:
    assert editable_fields(TaskState.READY) == list(EDITABLE)
    assert editable_fields(TaskState.CLAIMED) == ["priority"]
    assert editable_fields(TaskState.SUCCEEDED) == []
    assert editable_fields(TaskState.READY, archived=True) == []


def test_title_priority_and_difficulty_cannot_be_cleared() -> None:
    assert set(EDITABLE) - {"title", "priority", "difficulty"} == CLEARABLE


def test_the_patch_tells_omitted_from_null_and_collects_unknown_keys() -> None:
    changes = TaskChanges.model_validate({"spec": None, "state": "done", "zzz": 1})
    assert changes.given() == {"spec": None}
    assert changes.unknown() == ["state", "zzz"]
    assert TaskChanges().given() == {}
