"""Event types and data shapes written to the events table."""

from typing import Any

from pydantic import BaseModel

TASK_COMMAND_BLOCKED = "task.command_blocked"


class CommandBlocked(BaseModel):
    """Data of a `task.command_blocked` event; the event subject carries the task."""

    tool: str  # the blocked tool, e.g. "Bash"
    rule: str  # closed rule id, e.g. "rm_root"
    reason: str  # human reason text

    def to_data(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "CommandBlocked":
        return cls.model_validate(data)
