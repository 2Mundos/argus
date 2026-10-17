"""Dedicated Claude login: the official client's `setup-token`, driven on a
pseudo-terminal for one Argus account (claude accounts spec 3). Sessions live in
memory only.

The client runs in a private temporary HOME. Its terminal output is parsed, never
logged or returned: the sign-in link and the pasted code exist only in the session
for the admin who started it, and the token only in the SOPS record at the end.
"""

import asyncio
import contextlib
import fcntl
import os
import pty
import re
import struct
import tempfile
import termios
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from argus_core import claude_auth

from argus_api.accounts import service
from argus_api.accounts.login_base import (
    LOGIN_TTL_S,
    START_TIMEOUT_S,
    LoginFailure,
    LoginManager,
    LoginSession,
    LoginState,
)
from argus_api.db import utcnow
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError

ALLOWED_HOSTS = ("claude.ai", "claude.com", "anthropic.com")
MAX_CODE_LEN = 2048
_CODE = re.compile(r"[A-Za-z0-9._~%+/=-]+#[A-Za-z0-9._~#%+/=-]+")  # code#state
_MAX_OUTPUT = 128 * 1024
_RESULT_TIMEOUT_S = 90.0  # from the code to the token, else the code was refused
MAX_ATTEMPTS = 3  # codes the client is given per login
_ENTER_DELAY_S = 0.25  # the client takes a long chunk as a paste and drops its Enter
_RELINK_TIMEOUT_S = 10.0  # from our retry Enter to the client's new sign-in link
_COLUMNS = 1000  # wide enough that the client never wraps the link or the token

_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (incl. hyperlinks)
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)
_URL = re.compile(r"https://[^\s\"'<>\x00-\x1f]+(?=\s)")
_TOKEN = re.compile(r"sk-ant-oat\d{0,4}-[A-Za-z0-9_-]{40,}(?=[^A-Za-z0-9_-]|$)")
# Fixed markers of the client's own messages. The TUI separates words with cursor
# moves, so spaces are optional. Only the matched marker is ever turned into a
# code (plus the three-digit status); no output is kept or returned.
_OAUTH_ERROR = re.compile(r"OAuth\s*error:?.*?status\s*code\s*(\d{3})", re.S)
_CODE_INVALID = re.compile(r"Invalid\s*code|Authentication\s*failed", re.I)
_RETRY = re.compile(r"Press\s*Enter\s*to\s*retry", re.I)
_SUCCESS = re.compile(r"Long-lived\s*authentication\s*token\s*created\s*successfully")

__all__ = ["ClaudeLoginManager", "LoginState", "LoginUnavailableError", "url_allowed"]


class LoginUnavailableError(ConflictError):
    code = "claude_login_unavailable"


@dataclass
class _Session(LoginSession):
    renew_id: str | None = None
    url: str | None = None
    attempts: int = 0  # codes handed to the client
    awaiting: bool = False  # the last code has no verdict yet
    refusal: str | None = None  # why the client turned the last code down
    mark: int = 0  # where the last code's output starts in `out`
    code_deadline: float | None = None
    need_enter: bool = False  # the client refused a code and waits for Enter
    relinking: bool = False  # our Enter was sent; the client prints a new link
    relink_mark: int = 0
    relink_deadline: float = 0.0
    master: int | None = None
    out: bytearray = field(default_factory=bytearray)
    eof: bool = False
    activity: asyncio.Event = field(default_factory=asyncio.Event)


def url_allowed(url: str) -> bool:
    """https on an Anthropic host, no credentials in the URL."""
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


def _plain(raw: bytes | bytearray) -> str:
    return _ANSI.sub("", bytes(raw).decode("utf-8", "replace")).replace("\r", "")


def _text(s: _Session) -> str:
    return _plain(s.out)


def _refusal(text: str) -> str | None:
    """A closed reason code for the client's refusal of a code, else None."""
    if m := _OAUTH_ERROR.search(text):
        return f"oauth_{m.group(1)}"
    if _CODE_INVALID.search(text):
        return "code_invalid"
    return None


_STUB = "#!/bin/sh\nexit 0\n"


class ClaudeLoginManager(LoginManager[_Session]):
    # -- public --------------------------------------------------------------------

    def configured(self) -> bool:
        return bool(self._settings.claude_login_image or self._settings.claude_bin)

    async def start(
        self, owner: str, actor: str, name: str | None, renew_id: str | None = None
    ) -> dict[str, Any]:
        if not self.configured():
            raise LoginUnavailableError(
                "no Claude client is configured: set ARGUS_CLAUDE_LOGIN_IMAGE or "
                "ARGUS_CLAUDE_BIN"
            )
        if renew_id is not None:
            async with self._db.read() as conn:
                account = await service.get_account(conn, renew_id, utcnow())
            if account.provider != claude_auth.PROVIDER:
                raise NotFoundError("no anthropic account with that id")
        self._admit(owner)
        session = _Session(
            id=uuid4().hex,
            owner=owner,
            name=(name or "Claude (subscription login)").strip()[:100],
            expires_at=self._clock() + LOGIN_TTL_S,
            renew_id=renew_id,
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

    async def submit_code(self, login_id: str, owner: str, code: str) -> dict[str, Any]:
        """Hand the operator's code to the client, exactly as given, in one write
        like a paste. The client may refuse it and wait, so a few tries are allowed."""
        s = self._session(login_id, owner)
        self._expire_if_due(s)
        if s.state is not LoginState.PENDING or s.commit is not None:
            raise ConflictError("this login is no longer waiting", code="login_closed")
        if s.url is None or s.master is None or s.eof:
            raise ConflictError(
                "no code can be accepted now", code="login_not_waiting_for_code"
            )
        if s.attempts >= MAX_ATTEMPTS:
            raise ConflictError(
                "too many codes were tried; start a new login",
                code="login_attempts_exhausted",
            )
        if not (0 < len(code) <= MAX_CODE_LEN and _CODE.fullmatch(code)):
            raise ValidationFailedError("the code has an invalid format")
        if s.awaiting or s.relinking:
            raise ConflictError(
                "the client has not answered the previous code yet",
                code="login_code_in_flight",
            )
        s.attempts += 1
        s.awaiting, s.refusal, s.need_enter = True, None, False
        s.mark = len(s.out)
        s.code_deadline = self._clock() + _RESULT_TIMEOUT_S
        master = s.master
        try:
            # Two writes: the client's input takes a long chunk as a paste and
            # swallows an Enter that arrives in it (measured, spec 3).
            os.write(master, code.encode("ascii"))
            await asyncio.sleep(_ENTER_DELAY_S)
            if s.master is not master:  # torn down while we waited
                return self.view(s)
            os.write(master, b"\r")
        except OSError:
            await self._finish(s, LoginState.FAILED, "client_exited")
        return self.view(s)

    def view(self, s: _Session) -> dict[str, Any]:
        pending = s.state is LoginState.PENDING
        return {
            "login_id": s.id,
            "state": s.state.value,
            "phase": self._phase(s) if pending else None,
            "attempts": s.attempts,
            "max_attempts": MAX_ATTEMPTS,
            "refusal": s.refusal if pending else None,
            "url": s.url if pending and not s.relinking else None,
            "expires_in_s": max(0, int(s.expires_at - self._clock())) if pending else 0,
            "account_id": s.account_id,
            "error": s.error,
        }

    # -- internals -----------------------------------------------------------------

    @staticmethod
    def _phase(s: _Session) -> str:
        if s.relinking:
            return "renewing_link"
        return "finishing" if s.awaiting else "waiting_for_code"

    def _scrub(self, s: _Session) -> None:
        s.out.clear()
        s.url = None

    async def _release(self, s: _Session) -> None:
        master, s.master = s.master, None
        if master is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(master)
            with contextlib.suppress(OSError):
                os.close(master)

    def _make_home(self) -> Path:
        base = self._settings.data_dir / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        base.chmod(0o700)
        home = Path(tempfile.mkdtemp(prefix="argus-claude-login-", dir=base))
        home.chmod(0o700)
        for sub in (".claude", ".config", ".local/state", ".local/share", ".cache"):
            (home / sub).mkdir(parents=True, mode=0o700, exist_ok=True)
        (home / "scratch").mkdir(mode=0o700)
        bin_dir = home / "bin"
        bin_dir.mkdir(mode=0o700)
        for stub in ("browser", "xdg-open", "open", "wslview"):
            (bin_dir / stub).write_text(_STUB)
            (bin_dir / stub).chmod(0o700)
        return home

    def _command(self, s: _Session) -> tuple[list[str], dict[str, str]]:
        assert s.home is not None
        s_ = self._settings
        env = {
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "NO_COLOR": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        if s_.claude_login_image:
            inner = "/claude-home"
            s.container = f"argus-claude-login-{s.id[:12]}"
            paths = self._paths(inner)
            argv = [
                "docker",
                "run",
                "--rm",
                "-it",
                "--name",
                s.container,
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory=1g",
                "--pids-limit=256",
                "-v",
                f"{s.home}:{inner}",
                "-w",
                inner,
            ]
            for key, value in {**env, **paths, "BROWSER": "/bin/true"}.items():
                argv += ["-e", f"{key}={value}"]
            argv += ["--entrypoint", "claude", s_.claude_login_image, "setup-token"]
            return argv, {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        assert s_.claude_bin
        paths = self._paths(str(s.home))
        env |= paths
        env["BROWSER"] = f"{s.home}/bin/browser"
        env["PATH"] = f"{s.home}/bin:{os.environ.get('PATH', '/usr/bin:/bin')}"
        return [s_.claude_bin, "setup-token"], env

    @staticmethod
    def _paths(root: str) -> dict[str, str]:
        return {
            "HOME": root,
            "CLAUDE_CONFIG_DIR": f"{root}/.claude",
            "XDG_CONFIG_HOME": f"{root}/.config",
            "XDG_STATE_HOME": f"{root}/.local/state",
            "XDG_DATA_HOME": f"{root}/.local/share",
            "XDG_CACHE_HOME": f"{root}/.cache",
            "TMPDIR": f"{root}/scratch",
        }

    def _on_readable(self, s: _Session) -> None:
        master = s.master
        if master is None:
            return
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the client closed its end
            data = b""
        if data:
            s.out += data
        else:
            s.eof = True
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(master)
        s.activity.set()

    async def _run(self, s: _Session, actor: str) -> None:
        try:
            s.home = self._make_home()
            argv, env = self._command(s)
            master, slave = pty.openpty()
            s.master = master
            os.set_blocking(master, False)
            fcntl.ioctl(
                slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, _COLUMNS, 0, 0)
            )
            try:
                s.proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    cwd=s.home,
                    start_new_session=True,
                )
            finally:
                os.close(slave)
            s.pgid = s.proc.pid  # a new session: its group id is its pid
            asyncio.get_running_loop().add_reader(master, self._on_readable, s)
            await asyncio.wait_for(self._drive(s, actor), LOGIN_TTL_S)
        except TimeoutError:
            await self._finish(s, LoginState.EXPIRED, "expired")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, LoginFailure) else type(exc).__name__
            await self._finish(s, LoginState.FAILED, reason)
        finally:
            s.ready.set()
            await self._kill(s)

    async def _drive(self, s: _Session, actor: str) -> None:
        while True:
            s.activity.clear()
            if len(s.out) > _MAX_OUTPUT:
                raise LoginFailure("output_too_large")
            text = _text(s)
            if s.url is None and (match := _URL.search(text)):
                if not url_allowed(match.group()):
                    raise LoginFailure("url_not_allowed")
                s.url = match.group()
                s.ready.set()
            if token := _TOKEN.search(text):
                await self._finished(s, actor, token.group())
                return
            if s.awaiting and (reason := _refusal(_plain(s.out[s.mark :]))):
                # Turned down, but the client waits for another code.
                s.awaiting, s.refusal, s.code_deadline = False, reason, None
                s.need_enter = True
            if s.need_enter and _RETRY.search(_plain(s.out[s.mark :])):
                # The client restarts the flow on Enter, with a new link and a new
                # verifier: send it ourselves, a code written now would be eaten by it.
                s.need_enter, s.relinking = False, True
                s.relink_mark = len(s.out)
                s.relink_deadline = self._clock() + _RELINK_TIMEOUT_S
                try:
                    assert s.master is not None
                    os.write(s.master, b"\r")
                except OSError:
                    raise LoginFailure("client_exited") from None
            if s.relinking:
                # A redraw may repeat the old link first: wait for a different one.
                links = _URL.findall(_plain(s.out[s.relink_mark :]))
                if fresh := next((u for u in links if u != s.url), None):
                    if not url_allowed(fresh):
                        raise LoginFailure("url_not_allowed")
                    s.url, s.relinking = fresh, False
                elif self._clock() >= s.relink_deadline:
                    s.relinking = False  # it kept its link
            if s.eof:
                if s.state is not LoginState.PENDING:
                    return
                raise LoginFailure(self._ended(s, text))
            timeout = None
            if s.code_deadline is not None:
                timeout = s.code_deadline - self._clock()
                if timeout <= 0:
                    raise LoginFailure(
                        "token_not_found" if _SUCCESS.search(text) else "no_token"
                    )
            if s.relinking:
                wake = max(0.0, s.relink_deadline - self._clock())
                timeout = wake if timeout is None else min(timeout, wake)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(s.activity.wait(), timeout)

    @staticmethod
    def _ended(s: _Session, text: str) -> str:
        """Why the client left without a token: a closed code, never its words."""
        if _SUCCESS.search(text):
            return "token_not_found"  # it said it succeeded; we saw no token
        if s.attempts:
            return _refusal(_plain(s.out[s.mark :])) or s.refusal or "login_rejected"
        return "client_exited" if s.url else "no_url"

    async def _finished(self, s: _Session, actor: str, token: str) -> None:
        doc = {"oauth_token": token}
        try:
            summary = claude_auth.validate(doc)
        except claude_auth.ClaudeAuthError:
            raise LoginFailure("token_format") from None
        if s.renew_id is None:
            await self._complete(
                s,
                lambda: self._persist_new(
                    s, actor, claude_auth.PROVIDER, summary.kind, doc, summary.mode
                ),
            )
        else:
            renew_id = s.renew_id
            await self._complete(
                s, lambda: self._persist_renewal(s, actor, renew_id, doc, summary)
            )

    async def _persist_renewal(
        self,
        s: _Session,
        actor: str,
        account_id: str,
        doc: dict[str, Any],
        summary: claude_auth.ClaudeAuthSummary,
    ) -> None:
        try:
            async with self._db.read() as conn:
                name = await service.secret_name_for_update(conn, account_id, None)
            previous = self._secrets.get(name)
            await self._secrets.put(name, doc)
            try:
                async with self._db.write() as tx:
                    await service.credential_renewed(
                        tx, account_id, summary.kind, summary.mode, actor
                    )
            except BaseException:
                # The row did not change: the old credential must stay in force.
                with contextlib.suppress(Exception):
                    if previous is None:
                        await self._secrets.delete(name)
                    else:
                        await self._secrets.put(name, previous)
                raise
        finally:
            s.commit = None
        s.account_id = account_id
        s.state, s.ended_at = LoginState.COMPLETED, self._clock()
