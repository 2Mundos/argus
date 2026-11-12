"""Event types and data shapes written to the events table."""

from typing import Any

from pydantic import BaseModel

TASK_COMMAND_BLOCKED = "task.command_blocked"

# Closed rule id -> reason of the command guard. The reason text is the one the hook
# prints; `integrations/nanoclaw/tests/test_command_guard.py` keeps this equal to
# `command_guard.REASONS`, and the channel's TypeScript table is checked the same way.
COMMAND_GUARD_REASONS = {
    "commit_attribution": (
        "a commit message must not contain a Co-Authored-By or other co-author "
        "attribution trailer (remove the trailer; git author and committer is enough)"
    ),
    "rm_root": "recursive deletion of /, ~, $HOME, /home/* or /root",
    "no_preserve_root": "--no-preserve-root disables the safety check of rm",
    "mkfs": "creating a filesystem destroys the data on the device",
    "dd_device": "writing to a block device overwrites a disk",
    "device_redirect": "redirecting output to a block device overwrites a disk",
    "fork_bomb": "a fork bomb exhausts the machine",
    "kubeconfig_secret": (
        "the task's kubeconfig holds a credential: use it only through "
        "kubectl --kubeconfig, never print, copy or decode it"
    ),
}


class CommandBlocked(BaseModel):
    """Data of a `task.command_blocked` event; the event subject carries the task."""

    tool: str  # the blocked tool, e.g. "Bash"
    rule: str  # closed rule id, e.g. "rm_root"
    reason: str  # human reason text
    attempt: int  # the attempt that ran the command
    command: str  # redacted, cleaned and cut; never the raw command

    def to_data(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "CommandBlocked":
        return cls.model_validate(data)
