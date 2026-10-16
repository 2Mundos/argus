"""Machinery shared by the dedicated-login managers (Codex, Claude): in-memory
sessions with owner, TTL and limits, one shielded teardown that every caller
waits for, and an account commit that cancel/expiry cannot interrupt.

A manager adds only how its client is started and driven. Nothing here reads
client output; reason codes are fixed strings.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from argus_core.accounts import AccountCreate

from argus_api.accounts import service
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.errors import ConflictError, NotFoundError
from argus_api.events import record
from argus_api.secrets import SecretStore

log = logging.getLogger(__name__)

LOGIN_TTL_S = 15 * 60
MAX_SESSIONS = 2
START_TIMEOUT_S = 30.0
_REAP_AFTER_S = 3600
_TERM_GRACE_S = 2.0


class LoginState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class LoginFailure(Exception):
    """A fixed reason code, safe to show; never built from client output."""


@dataclass
class LoginSession:
    id: str
    owner: str
    name: str
    expires_at: float
    state: LoginState = LoginState.PENDING
    account_id: str | None = None
    error: str | None = None
    ended_at: float | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    proc: asyncio.subprocess.Process | None = None
    home: Path | None = None
    container: str | None = None
    pgid: int | None = None  # the process group this manager created, never another
    commit: asyncio.Task[None] | None = None  # set while the account is being persisted
    cleanup: asyncio.Task[None] | None = None  # the one teardown in flight, if any


def signal_group(pgid: int, sig: int) -> bool:
    """Signal a group we created; False once it no longer exists."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class LoginManager[S: LoginSession]:
    def __init__(
        self,
        db: Database,
        secrets: SecretStore,
        settings: Settings,
        *,
        clock=time.monotonic,
    ) -> None:
        self._db = db
        self._secrets = secrets
        self._settings = settings
        self._clock = clock
        self._sessions: dict[str, S] = {}
        self._cleanups: set[asyncio.Task[None]] = set()

    # -- public --------------------------------------------------------------------

    def get(self, login_id: str, owner: str) -> dict[str, Any]:
        s = self._session(login_id, owner)
        self._expire_if_due(s)
        return self.view(s)

    async def cancel(self, login_id: str, owner: str) -> dict[str, Any]:
        s = self._session(login_id, owner)
        if s.state is LoginState.PENDING:
            await self._finish(s, LoginState.CANCELLED, None)
        return self.view(s)

    async def shutdown(self) -> None:
        for s in list(self._sessions.values()):
            if s.state is LoginState.PENDING:
                await self._finish(s, LoginState.CANCELLED, "shutdown")
        if self._cleanups:
            await asyncio.gather(*self._cleanups, return_exceptions=True)

    def view(self, s: S) -> dict[str, Any]:
        raise NotImplementedError

    # -- internals -----------------------------------------------------------------

    def _admit(self, owner: str) -> None:
        """Room for one more login for this owner, or a 409."""
        self._reap()
        live = [s for s in self._sessions.values() if s.state is LoginState.PENDING]
        if len(live) >= MAX_SESSIONS:
            raise ConflictError("too many logins in progress", code="login_limit")
        if any(s.owner == owner for s in live):
            raise ConflictError(
                "a login is already in progress for this token; cancel it first",
                code="login_in_progress",
            )

    def _session(self, login_id: str, owner: str) -> S:
        s = self._sessions.get(login_id)
        # Another caller's session is indistinguishable from a missing one.
        if s is None or s.owner != owner:
            raise NotFoundError("login not found")
        return s

    def _expire_if_due(self, s: S) -> None:
        """Expire a pending login whose time is up and stop its client. Not while
        the account is being persisted: that outcome is already decided."""
        if s.state is not LoginState.PENDING or s.commit is not None:
            return
        if self._clock() < s.expires_at:
            return
        s.state, s.error, s.ended_at = LoginState.EXPIRED, "expired", self._clock()
        cleanup = asyncio.create_task(self._finish(s, LoginState.EXPIRED, "expired"))
        self._cleanups.add(cleanup)
        cleanup.add_done_callback(self._cleanups.discard)

    def _reap(self) -> None:
        now = self._clock()
        for key, s in list(self._sessions.items()):
            if s.ended_at is not None and now - s.ended_at > _REAP_AFTER_S:
                del self._sessions[key]

    async def _finish(self, s: S, state: LoginState, error: str | None) -> None:
        if s.commit is not None:
            # Persisting is past the point of no return: let it settle so the
            # outcome is `completed` with its account, never `cancelled` with one.
            await asyncio.wait({s.commit})
        if s.state is LoginState.PENDING:
            s.state, s.error, s.ended_at = state, error, self._clock()
        await self._kill(s)
        s.ready.set()
        if s.task is not None and s.task is not asyncio.current_task():
            s.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await s.task

    async def _kill(self, s: S) -> None:
        """Tear the session's client down. One teardown runs at a time and every
        caller waits for it to finish (container removed included); it is shielded,
        so cancelling a caller never abandons a half-done `docker rm`."""
        task = s.cleanup
        if task is None:
            task = s.cleanup = asyncio.create_task(self._teardown(s))
            self._cleanups.add(task)
            task.add_done_callback(self._cleanups.discard)
            task.add_done_callback(
                lambda t: setattr(s, "cleanup", None) if s.cleanup is t else None
            )
        await asyncio.shield(task)

    def _scrub(self, s: S) -> None:
        """Drop session secrets held in memory (a subclass adds its own)."""

    async def _teardown(self, s: S) -> None:
        proc, s.proc = s.proc, None
        pgid, s.pgid = s.pgid, None
        if proc is not None:
            # The client (an npm wrapper, docker) may have children: stop the whole
            # group, politely first, then for good.
            if pgid is not None and signal_group(pgid, signal.SIGTERM):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), _TERM_GRACE_S)
                signal_group(pgid, signal.SIGKILL)
            elif proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        await self._release(s)
        container, s.container = s.container, None
        if container:
            rm = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                container,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(rm.wait(), 15)
            except Exception:  # best effort; do not leave `docker rm` behind
                with contextlib.suppress(ProcessLookupError):
                    rm.kill()
        if s.home is not None:
            shutil.rmtree(s.home, ignore_errors=True)
            s.home = None
        self._scrub(s)

    async def _release(self, s: S) -> None:
        """Free per-session resources other than the process and its home."""

    async def _complete(
        self, s: S, persist: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        # No await from the expiry check to setting `commit`: the login is either
        # already over (cancelled, expired: no account, no secret) or it is
        # committed, and from then on cancel/expiry wait for the outcome instead
        # of racing it.
        self._expire_if_due(s)
        if s.state is not LoginState.PENDING:
            return
        s.commit = asyncio.create_task(persist())
        # Shielded: cancelling this task must not abandon a half-written account.
        await asyncio.shield(s.commit)

    async def _persist_new(
        self,
        s: S,
        actor: str,
        provider: str,
        kind: str,
        doc: dict[str, Any],
        mode: str,
    ) -> None:
        account_id = str(uuid4())
        secret = f"account-{account_id}"
        try:
            await self._secrets.put(secret, doc)
            body = AccountCreate(
                name=f"{s.name} {account_id[:8]}",
                provider=provider,
                kind=kind,  # type: ignore[arg-type]
                secret_ref=f"sops:{secret}",
            )
            async with self._db.write() as tx:
                await service.create_account(
                    tx, account_id, body, f"sops:{secret}", actor, self._secrets
                )
                await record(
                    tx,
                    actor,
                    "account",
                    account_id,
                    "account.login_completed",
                    {"mode": mode},
                )
        except BaseException:
            # No account row means no secret left behind.
            with contextlib.suppress(Exception):
                await self._secrets.delete(secret)
            raise
        finally:
            s.commit = None
        s.account_id = account_id
        s.state, s.ended_at = LoginState.COMPLETED, self._clock()
