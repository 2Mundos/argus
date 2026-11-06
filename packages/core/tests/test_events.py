import json

import pytest
from argus_core.events import TASK_COMMAND_BLOCKED, CommandBlocked
from pydantic import ValidationError


def test_command_blocked_round_trips_through_json() -> None:
    event = CommandBlocked(
        tool="Bash",
        rule="rm_root",
        reason="Refusing to delete /",
        attempt=1,
        command="rm -rf /",
    )
    stored = json.loads(json.dumps(event.to_data()))
    assert CommandBlocked.from_data(stored) == event
    assert TASK_COMMAND_BLOCKED == "task.command_blocked"


def test_command_blocked_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        CommandBlocked.from_data({"tool": "Bash", "reason": "x"})
