"""Dedicated Codex login: the official client's device-code flow, driven for one
Argus account (codex accounts spec 3). Sessions live in memory only.

The client runs `app-server` over stdio in a private temporary CODEX_HOME. Its
output is parsed, never logged: the user code and tokens exist only in the
session view for the admin who started it and in the SOPS record at the end.
The session machinery is shared with the Claude login (`login_base`).
"""

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from argus_core import codex_auth

from argus_api.accounts.login_base import (
    LOGIN_TTL_S,
    START_TIMEOUT_S,
    LoginFailure,
    LoginManager,
    LoginSession,
    LoginState,
)
from argus_api.errors import ConflictError

ALLOWED_HOSTS = ("auth.openai.com", "chatgpt.com", "openai.com")

__all__ = ["CodexLoginManager", "LoginState", "LoginUnavailableError", "url_allowed"]


class LoginUnavailableError(ConflictError):
    code = "codex_login_unavailable"


_Failure = LoginFailure


@dataclass
class _Session(LoginSession):
    verification_url: str | None = None
    user_code: str | None = None


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


class CodexLoginManager(LoginManager[_Session]):
    # -- public --------------------------------------------------------------------

    def configured(self) -> bool:
        return bool(self._settings.codex_login_image or self._settings.codex_bin)

    async def start(self, owner: str, actor: str, name: str | None) -> dict[str, Any]:
        if not self.configured():
            raise LoginUnavailableError(
                "no Codex client is configured: set ARGUS_CODEX_LOGIN_IMAGE or ARGUS_CODEX_BIN"
            )
        self._admit(owner)
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

    def _scrub(self, s: _Session) -> None:
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

    async def _complete(self, s: _Session, actor: str) -> None:  # ty: ignore[invalid-method-override]
        assert s.home is not None
        doc = json.loads((s.home / "auth.json").read_text())
        if not isinstance(doc, dict):
            raise _Failure("auth_json")
        summary = codex_auth.validate(doc)
        await super()._complete(
            s,
            lambda: self._persist_new(
                s, actor, codex_auth.PROVIDER, summary.kind, doc, summary.mode
            ),
        )
