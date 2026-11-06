"""Command guard denials as `task.command_blocked` events
(docs/specs/command-guard.spec.md 3).

The channel reads a denial from the Claude transcript and reports it here. A denial is
informational: it changes no task state, makes no failure class and no inbox item.
"""

from typing import Annotated

import aiosqlite
from argus_core.events import (
    COMMAND_GUARD_REASONS,
    TASK_COMMAND_BLOCKED,
    CommandBlocked,
)
from argus_core.redact import redact, redact_private_keys
from pydantic import BaseModel, Field, StringConstraints

from argus_api.db import Snapshot, Tx, ts
from argus_api.errors import ValidationFailedError
from argus_api.events import record
from argus_api.tasks import service
from argus_api.tasks.log import _CONTROL

MAX_COMMAND_CHARS = 200  # stored; longer is cut with "…"
MAX_INPUT_CHARS = 16_000  # accepted; the redaction runs over all of it first
MAX_PER_ATTEMPT = 20  # events one attempt may record; a looping agent adds none after

ToolUseId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")]


class CommandBlockedWrite(BaseModel):
    attempt: int
    tool_use_id: ToolUseId
    rule: str = Field(max_length=100)
    command: str = Field(max_length=MAX_INPUT_CHARS)


class CommandBlockedAccepted(BaseModel):
    # "recorded", "duplicate" (this denial is already an event) or "dropped" (the
    # attempt is over its limit; no event). Both of the latter are final: do not retry.
    status: str


def clean_command(command: str, secrets: tuple[str, ...] = ()) -> str:
    """One line: control characters removed, credentials masked, whitespace collapsed,
    cut at MAX_COMMAND_CHARS. Redaction runs before the cut, so a cut never leaves half
    a pattern-matched secret."""
    text = redact(redact_private_keys(_CONTROL.sub("", command)), secrets)
    text = " ".join(text.split())
    if len(text) > MAX_COMMAND_CHARS:
        text = text[: MAX_COMMAND_CHARS - 1] + "…"
    return text


def check_rule(rule: str) -> None:
    if rule not in COMMAND_GUARD_REASONS:
        raise ValidationFailedError(
            f"unknown command guard rule: {rule[:50]!r}",
            code="unknown_rule",
            details=sorted(COMMAND_GUARD_REASONS),
        )


async def admit(
    snap: Tx | Snapshot, agent_id: str, task_id: str, body: CommandBlockedWrite
) -> aiosqlite.Row:
    """The rule and lease checks of `store`, without writing; the route runs them on a
    read snapshot first, `store` repeats them in the writer."""
    check_rule(body.rule)
    return await service.check_lease(snap, task_id, body.attempt, agent_id)


async def store(
    tx: Tx,
    agent_id: str,
    task_id: str,
    body: CommandBlockedWrite,
    secrets: tuple[str, ...] = (),
) -> CommandBlockedAccepted:
    """Record one denial for the attempt the caller holds the live lease on. The reason
    comes from the server's own table, never from the caller. Does not renew the lease
    or touch the task."""
    await admit(tx, agent_id, task_id, body)
    async with tx.conn.execute(
        "SELECT tool_use_id FROM task_command_blocks WHERE task_id = ? AND attempt = ?",
        (task_id, body.attempt),
    ) as cur:
        seen = [row["tool_use_id"] for row in await cur.fetchall()]
    if body.tool_use_id in seen:
        return CommandBlockedAccepted(status="duplicate")
    if len(seen) >= MAX_PER_ATTEMPT:
        return CommandBlockedAccepted(status="dropped")
    await tx.conn.execute(
        "INSERT INTO task_command_blocks (task_id, attempt, tool_use_id, at)"
        " VALUES (?, ?, ?, ?)",
        (task_id, body.attempt, body.tool_use_id, ts(tx.now)),
    )
    event = CommandBlocked(
        tool="Bash",
        rule=body.rule,
        reason=COMMAND_GUARD_REASONS[body.rule],
        attempt=body.attempt,
        command=clean_command(body.command, secrets),
    )
    await record(
        tx, f"agent:{agent_id}", "task", task_id, TASK_COMMAND_BLOCKED, event.to_data()
    )
    return CommandBlockedAccepted(status="recorded")
