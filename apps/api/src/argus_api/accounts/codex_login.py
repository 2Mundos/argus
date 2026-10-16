"""Dedicated Codex login: the official client's device-code flow, driven for one
Argus account (codex accounts spec 3). Sessions live in memory only.

The client runs `app-server` over stdio in a private temporary CODEX_HOME. Its
output is parsed, never logged: the user code and tokens exist only in the
session view for the admin who started it and in the SOPS record at the end.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from argus_core import codex_auth
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
ALLOWED_HOSTS = ("auth.openai.com", "chatgpt.com", "openai.com")
_REAP_AFTER_S = 3600
_TERM_GRACE_S = 2.0


class LoginState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class LoginUnavailableError(ConflictError):
    code = "codex_login_unavailable"


class _Failure(Exception):
    """A fixed reason code, safe to show; never built from client output."""


@dataclass
class _Session:
    id: str
    owner: str
    name: str
    expires_at: float
    state: LoginState = LoginState.PENDING
    verification_url: str | None = None
    user_code: str | None = None
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


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal a group we created; False once it no longer exists."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def url_allowed(url: str) -> bool:
    """https on an OpenAI host, no credentials in the URL."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    )


class CodexLoginManager:
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
        self._sessions: dict[str, _Session] = {}
        self._cleanups: set[asyncio.Task[None]] = set()

    # -- public --------------------------------------------------------------------

    def configured(self) -> bool:
        return bool(self._settings.codex_login_image or self._settings.codex_bin)

    async def start(self, owner: str, actor: str, name: str | None) -> dict[str, Any]:
        if not self.configured():
            raise LoginUnavailableError(
                "no Codex client is configured: set ARGUS_CODEX_LOGIN_IMAGE or ARGUS_CODEX_BIN"
            )
        self._reap()
        live = [s for s in self._sessions.values() if s.state is LoginState.PENDING]
        if len(live) >= MAX_SESSIONS:
            raise ConflictError("too many logins in progress", code="login_limit")
        if any(s.owner == owner for s in live):
            raise ConflictError(
                "a login is already in progress for this token; cancel it first",
                code="login_in_progress",
            )
        session = _Session(
            id=uuid4().hex,
            owner=owner,
            name=(name or "Codex (ChatGPT login)").strip()[:100],
            expires_at=self._clock() + LOGIN_TTL_S,
        )
        self._sessions[session.id] = session
        session.task = asyncio.create_task(self._run(session, actor))
        try:
            await asyncio.wait_for(session.ready.wait(), START_TIMEOUT_S)
        except TimeoutError:
            await self._finish(session, LoginState.FAILED, "start_timeout")
        if session.state is not LoginState.PENDING:
            raise LoginUnavailableError(f"login did not start ({session.error})")
        return self.view(session)

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

    def view(self, s: _Session) -> dict[str, Any]:
        pending = s.state is LoginState.PENDING
        return {
            "login_id": s.id,
            "state": s.state.value,
            "verification_url": s.verification_url if pending else None,
            "user_code": s.user_code if pending else None,
            "expires_in_s": max(0, int(s.expires_at - self._clock())) if pending else 0,
            "account_id": s.account_id,
            "error": s.error,
        }

    # -- internals -----------------------------------------------------------------

    def _session(self, login_id: str, owner: str) -> _Session:
        s = self._sessions.get(login_id)
        # Another caller's session is indistinguishable from a missing one.
        if s is None or s.owner != owner:
            raise NotFoundError("login not found")
        return s

    def _expire_if_due(self, s: _Session) -> None:
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

    async def _finish(self, s: _Session, state: LoginState, error: str | None) -> None:
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

    async def _kill(self, s: _Session) -> None:
        proc, s.proc = s.proc, None
        pgid, s.pgid = s.pgid, None
        if proc is not None:
            # The client (the npm wrapper) may have native children: stop the whole
            # group, politely first, then for good.
            if pgid is not None and _signal_group(pgid, signal.SIGTERM):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), _TERM_GRACE_S)
                _signal_group(pgid, signal.SIGKILL)
            elif proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
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
        s.user_code = None

    def _command(self, s: _Session) -> tuple[list[str], dict[str, str]]:
        assert s.home is not None
        cfg = ["-c", 'cli_auth_credentials_store="file"']
        s_ = self._settings
        if s_.codex_login_image:
            s.container = f"argus-codex-login-{s.id[:12]}"
            return (
                [
                    "docker",
                    "run",
                    "--rm",
                    "-i",
                    "--name",
                    s.container,
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--memory=512m",
                    "-v",
                    f"{s.home}:/codex-home",
                    "-e",
                    "CODEX_HOME=/codex-home",
                    "--entrypoint",
                    "codex",
                    s_.codex_login_image,
                    "app-server",
                    *cfg,
                ],
                {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        assert s_.codex_bin
        return (
            [s_.codex_bin, "app-server", *cfg],
            {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "CODEX_HOME": str(s.home),
                "HOME": str(s.home),
            },
        )

    async def _run(self, s: _Session, actor: str) -> None:
        try:
            s.home = Path(tempfile.mkdtemp(prefix="argus-codex-login-"))
            s.home.chmod(0o700)
            argv, env = self._command(s)
            s.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            s.pgid = s.proc.pid  # a new session: its group id is its pid
            await asyncio.wait_for(self._drive(s, actor), LOGIN_TTL_S)
        except TimeoutError:
            await self._finish(s, LoginState.EXPIRED, "expired")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, _Failure) else type(exc).__name__
            await self._finish(s, LoginState.FAILED, reason)
        finally:
            s.ready.set()
            await self._kill(s)

    async def _drive(self, s: _Session, actor: str) -> None:
        proc = s.proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        stdin, stdout = proc.stdin, proc.stdout

        async def send(msg: dict[str, Any]) -> None:
            stdin.write((json.dumps(msg) + "\n").encode())
            await stdin.drain()

        async def messages():
            while line := await stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    yield msg

        await send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "argus", "title": "Argus", "version": "1"}
                },
            }
        )
        started = False
        async for msg in messages():
            if msg.get("id") == 1:
                if "error" in msg:
                    raise _Failure("initialize")
                await send({"method": "initialized"})
                await send(
                    {
                        "id": 2,
                        "method": "account/login/start",
                        "params": {"type": "chatgptDeviceCode"},
                    }
                )
            elif msg.get("id") == 2:
                result = msg.get("result")
                if (
                    not isinstance(result, dict)
                    or result.get("type") != "chatgptDeviceCode"
                ):
                    raise _Failure("login_start")
                url, code = result.get("verificationUrl"), result.get("userCode")
                if not (isinstance(url, str) and isinstance(code, str) and code):
                    raise _Failure("login_start")
                if not url_allowed(url):
                    raise _Failure("url_not_allowed")
                s.verification_url, s.user_code = url, code
                started = True
                s.ready.set()
            elif msg.get("method") == "account/login/completed":
                if not started:
                    raise _Failure("login_start")
                params = msg.get("params") or {}
                if params.get("success") is not True:
                    await self._finish(s, LoginState.FAILED, "login_rejected")
                    return
                await self._complete(s, actor)
                return
        if s.state is LoginState.PENDING:
            raise _Failure("client_exited")

    async def _complete(self, s: _Session, actor: str) -> None:
        assert s.home is not None
        doc = json.loads((s.home / "auth.json").read_text())
        if not isinstance(doc, dict):
            raise _Failure("auth_json")
        summary = codex_auth.validate(doc)
        # No await from this check to setting `commit`: the login is either already
        # over (cancelled, expired: no account, no secret) or it is committed, and
        # from then on cancel/expiry wait for the outcome instead of racing it.
        self._expire_if_due(s)
        if s.state is not LoginState.PENDING:
            return
        s.commit = asyncio.create_task(self._persist(s, actor, doc, summary))
        # Shielded: cancelling this task must not abandon a half-written account.
        await asyncio.shield(s.commit)

    async def _persist(
        self,
        s: _Session,
        actor: str,
        doc: dict[str, Any],
        summary: codex_auth.CodexAuthSummary,
    ) -> None:
        account_id = str(uuid4())
        secret = f"account-{account_id}"
        try:
            await self._secrets.put(secret, doc)
            body = AccountCreate(
                name=f"{s.name} {account_id[:8]}",
                provider=codex_auth.PROVIDER,
                kind=summary.kind,  # type: ignore[arg-type]
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
                    {"mode": summary.mode},
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
