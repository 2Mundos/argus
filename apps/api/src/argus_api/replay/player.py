"""Play a fixture back through the MCP tools a worker uses.

`Call` is `await call(tool_name, **arguments) -> result`; it raises `CallError` when
the tool answers with an error. The player owns no transport, clock or sleep, so a
test drives it against the real API with a fake clock.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from argus_api.replay import POOL

Call = Callable[..., Awaitable[Any]]
Sleep = Callable[[float], Awaitable[None]]

LOG_BATCH_S = 1.0  # the API takes one log batch per second per source
MAX_LOG_LINES = 200
HEARTBEAT_S = 10.0  # real seconds between heartbeats while waiting
RETRIES = 5
_RETRY_IN = re.compile(r"retry in ([\d.]+) s")
_STATUS = re.compile(r"\b(409|429) \w+")


class CallError(Exception):
    """A tool answered with an error; `text` is the API's description."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class NotReplayTask(Exception):
    """The claimed task does not belong to the replay pool."""


@dataclass
class Result:
    status: str  # complete | fail | cancelled
    task_id: str
    attempt: int
    played: int = 0
    steers: list[str] = field(default_factory=list)


async def claim(call: Call, *, wait_s: float = 20) -> dict | None:
    """The next task in the replay pool, or None when none became ready."""
    task = (await call("claim_task", capabilities=[POOL], wait_s=wait_s))["task"]
    if task is not None and POOL not in task["required_capabilities"]:
        # Untagged tasks match every worker; refuse instead of faking real work.
        raise NotReplayTask(
            f"task {task['id']} does not require the {POOL!r} capability;"
            " run replay only against a dev instance whose tasks are tagged"
        )
    return task


async def play(
    task: dict,
    fixture: dict,
    call: Call,
    sleep: Sleep,
    *,
    speed: float = 1.0,
    heartbeat_s: float = HEARTBEAT_S,
) -> Result:
    """Send the fixture's steps for the claimed `task`, in order, then finish it.

    Steers received on a heartbeat are written to the live log and acked on the next
    one; a lease the API rejects (cancel, release, expiry) or a 409 on a write stops
    playback with status `cancelled`."""
    if speed <= 0:
        raise ValueError("speed must be positive")
    run = _Run(task["id"], task["attempt"], call, sleep, speed, heartbeat_s)
    steps = fixture["steps"]
    try:
        await run.heartbeat()
        i = 0
        while i < len(steps):
            step = steps[i]
            i += 1
            if step["type"] == "log":
                batch = [step]
                while (
                    i < len(steps)
                    and steps[i]["type"] == "log"
                    and steps[i]["source"] == step["source"]
                    and len(batch) < MAX_LOG_LINES
                    and (steps[i]["t"] - step["t"]) / speed < LOG_BATCH_S
                ):
                    batch.append(steps[i])
                    i += 1
                await run.wait_until(batch[-1]["t"])
                await run.send(
                    "append_task_log",
                    lines=[s["line"] for s in batch],
                    source=step["source"],
                )
                run.played += len(batch)
                continue
            await run.wait_until(step["t"])
            if step["type"] == "progress":
                await run.send(
                    "report_progress",
                    message=step["message"],
                    percent=step.get("percent"),
                    kind=step.get("kind"),
                    data=step.get("data"),
                )
            elif step["type"] == "models":
                await run.send(
                    "report_task_models",
                    executions=step.get("executions"),
                    selections=step.get("selections"),
                )
            # "event" steps are audit rows the API writes itself; nothing to send.
            run.played += 1
        await run.heartbeat()
        final = fixture["final"]
        if final["outcome"] == "complete":
            await run.send("complete_task", **final["result"])
        else:
            await run.send("fail_task", error=final["error"])
        return run.result(final["outcome"])
    except _Lost:
        return run.result("cancelled")


class _Lost(Exception):
    pass


class _Run:
    def __init__(
        self,
        task_id: str,
        attempt: int,
        call: Call,
        sleep: Sleep,
        speed: float,
        heartbeat_s: float,
    ) -> None:
        self.task_id = task_id
        self.attempt = attempt
        self.call = call
        self.sleep = sleep
        self.speed = speed
        self.heartbeat_s = heartbeat_s
        self.t = 0.0  # fixture time reached
        self.since_beat = 0.0  # real seconds slept since the last heartbeat
        self.acks: list[str] = []
        self.steers: list[str] = []
        self.played = 0

    def result(self, status: str) -> Result:
        return Result(status, self.task_id, self.attempt, self.played, self.steers)

    async def wait_until(self, t: float) -> None:
        """Sleep the recorded gap divided by speed, heartbeating on the way."""
        remaining = max(t - self.t, 0.0) / self.speed
        self.t = max(t, self.t)
        beat = False
        while remaining > 0:
            chunk = remaining
            if self.heartbeat_s > 0:
                chunk = min(remaining, self.heartbeat_s - self.since_beat)
            await self.sleep(chunk)
            remaining -= chunk
            self.since_beat += chunk
            if self.since_beat >= self.heartbeat_s:
                await self.heartbeat()
                beat = True
        if self.heartbeat_s <= 0 and not beat:  # beat before every step
            await self.heartbeat()

    async def heartbeat(self) -> None:
        self.since_beat = 0.0
        beat = await self.call(
            "heartbeat",
            leases=[{"task_id": self.task_id, "attempt": self.attempt}],
            runtime={
                "worker_version": "replay",
                "engine": "replay",
                "features": ["steer"],
            },
            steer_acks=self.acks,
        )
        self.acks = []
        if any(
            r["task_id"] == self.task_id and r["attempt"] == self.attempt
            for r in beat["rejected"]
        ):
            raise _Lost
        for steer in beat.get("steers", []):
            if steer["task_id"] == self.task_id and steer["id"] not in self.acks:
                self.steers.append(steer["message"])
                self.acks.append(steer["id"])
                await self.send(
                    "append_task_log", lines=[f"[replay] steer: {steer['message']}"]
                )

    async def send(self, tool: str, **arguments: Any) -> Any:
        """Call a task-scoped tool, waiting out a rate limit the API reports."""
        for tries in range(RETRIES):
            try:
                return await self.call(
                    tool, task_id=self.task_id, attempt=self.attempt, **arguments
                )
            except CallError as err:
                status = _STATUS.search(err.text)
                if status and status[1] == "409":  # stale attempt, not claimed
                    raise _Lost from err
                m = _RETRY_IN.search(err.text)
                if not status or m is None or tries == RETRIES - 1:
                    raise
                await self.sleep(float(m[1]) + 0.1)
        raise AssertionError("unreachable")
