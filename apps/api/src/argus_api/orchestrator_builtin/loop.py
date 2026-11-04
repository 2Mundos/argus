"""The built-in instance: register, heartbeat, claim chat turns, drain and leave
(builtin-orchestrator spec 3.2, 3.3, 3.8).

One claim loop per instance long-polls the chat route and starts a task per
claimed turn; the server bounds how many turns run at once.
"""

import asyncio
import contextlib
from typing import Any

import httpx2 as httpx
from fastapi import FastAPI

from argus_api.fleet.targets import InProcessHandle, InProcessRunner
from argus_api.ops import VERSION
from argus_api.orchestrator_builtin import tools, turn
from argus_api.orchestrator_builtin.accounts import Engine, Sleep, sleep_or_stop
from argus_api.orchestrator_builtin.client import (
    BASE_URL,
    CLAIM_TIMEOUT_S,
    ArgusClient,
)

CLAIM_WAIT_S = 30
CLAIM_RETRY_S = 5.0
RUNTIME = {"worker_version": VERSION, "features": ["builtin"]}
CRASH_TEXT = "The orchestrator hit an internal error in this turn. Send it again."


class BuiltinLoop:
    def __init__(
        self,
        handle: InProcessHandle,
        argus: ArgusClient,
        models: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.handle = handle
        self.argus = argus
        self.models = models
        self.sleep = sleep
        self.stopping = handle.stopping  # set by stop, or by a draining heartbeat
        self.engine = Engine(argus, handle.log)
        self.turns: set[asyncio.Task[None]] = set()
        self.deps: turn.TurnDeps | None = None
        self.ended = False  # the identity ended on the server: nothing to leave

    async def run(self) -> None:
        await self.register()
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            await self.setup()
            await self._claim_loop()
            if self.turns:
                await asyncio.wait(self.turns)
        except asyncio.CancelledError:
            # The stop grace ran out: turns in flight reply that Argus stopped.
            for task in self.turns:
                task.cancel()
            await asyncio.gather(*self.turns, return_exceptions=True)
            raise
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if not self.ended:
                await self.argus.rest("POST", "/agents/me/leave")

    async def register(self) -> None:
        token = self.handle.env["ARGUS_BOOTSTRAP_TOKENS"].split(",")[0]
        res = await self.argus.rest(
            "POST", "/agents/register", json={"bootstrap_token": token}
        )
        if not res.ok:
            raise RuntimeError(f"registration answered {res.status} {res.code}")
        self.argus.token = res.body["token"]
        self.interval = float(res.body["heartbeat_interval_s"])
        self.handle.log("registered")

    async def setup(self) -> None:
        """The tools and the skill, read once for the life of the loop (3.3, 3.6)."""
        self.deps = turn.TurnDeps(
            argus=self.argus,
            models=self.models,
            engine=self.engine,
            tools=await tools.discover(self.argus),
            skill=await tools.read_skill(self.argus),
            sleep=self.sleep,
            log=self.handle.log,
        )

    async def _heartbeat(self) -> None:
        while not self.stopping.is_set():
            if await self._beat() == "stop":
                self.stopping.set()
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), self.interval)

    async def _beat(self) -> str | None:
        """One heartbeat; "stop" when the server drains or ended this identity."""
        try:
            res = await self.argus.rest(
                "POST", "/agents/me/heartbeat", json={"leases": [], "runtime": RUNTIME}
            )
        except httpx.HTTPError as exc:  # tried again at the next interval
            self.handle.log(f"error: heartbeat {type(exc).__name__}")
            return None
        if res.status in (401, 403, 404):
            self.ended = True
            return "stop"
        if not res.ok:
            return None
        self.interval = float(res.body["heartbeat_interval_s"])
        return "stop" if res.body.get("state") == "draining" else None

    async def _claim_loop(self) -> None:
        while not self.stopping.is_set():
            if self.engine.lease is None and not await self.engine.wait_for_lease(
                self.stopping, self.sleep
            ):
                return
            claim = await self.claim_once()
            if claim is None:
                continue
            task = asyncio.create_task(self._turn(claim))
            self.turns.add(task)
            task.add_done_callback(self.turns.discard)

    async def claim_once(self) -> dict[str, Any] | None:
        """One long poll, given up when the loop is asked to stop."""
        poll = asyncio.ensure_future(
            self.argus.rest(
                "GET",
                "/agents/me/chat",
                params={"wait_s": CLAIM_WAIT_S},
                read_s=CLAIM_TIMEOUT_S,
            )
        )
        stop = asyncio.ensure_future(self.stopping.wait())
        await asyncio.wait({poll, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if not poll.done():
            poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll
            return None
        try:
            res = poll.result()
        except httpx.HTTPError as exc:
            self.handle.log(f"error: claim {type(exc).__name__}")
            await sleep_or_stop(self.sleep, CLAIM_RETRY_S, self.stopping)
            return None
        if res.status in (401, 403):
            self.ended = True
            self.stopping.set()
            return None
        if not res.ok:
            self.handle.log(f"error: claim {res.status} {res.code}")
            await sleep_or_stop(self.sleep, CLAIM_RETRY_S, self.stopping)
            return None
        return res.body or None

    async def _turn(self, claim: dict[str, Any]) -> None:
        assert self.deps is not None
        try:
            await turn.run(self.deps, claim)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Type only: never a prompt, tool result or credential.
            self.handle.log(f"error: turn crashed ({type(exc).__name__})")
            await turn.post(
                self.deps,
                claim["conversation"]["id"],
                claim["message"]["turn_id"],
                CRASH_TEXT,
                None,
            )


def make_runner(app: FastAPI) -> InProcessRunner:
    """What the in-process target runs for each built-in instance. Tests set
    `app.state.model_transport` (a fake model API) and `app.state.builtin_sleep`."""

    async def runner(handle: InProcessHandle) -> None:
        # The reconciler may start a loop before MCP serves; the loop needs it.
        ready: asyncio.Event | None = getattr(app.state, "builtin_ready", None)
        if ready is not None:
            await ready.wait()
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE_URL
            ) as argus_http,
            httpx.AsyncClient(
                transport=getattr(app.state, "model_transport", None),
                follow_redirects=False,
            ) as model_http,
        ):
            await BuiltinLoop(
                handle,
                ArgusClient(argus_http),
                model_http,
                sleep=getattr(app.state, "builtin_sleep", asyncio.sleep),
            ).run()

    return runner
