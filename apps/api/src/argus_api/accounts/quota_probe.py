"""Quota probe and its background loop (account usage 3.3).

Each account kind with a read-only source has a reader: Codex (below), Claude's OAuth
usage and OpenRouter's key (`quota_readers`). A probe that cannot read says why with
a code, and never touches the last good figure. The Codex probe runs the official client's `app-server` over stdio in a throwaway
CODEX_HOME holding one account's `auth.json`, and reads its rate limits with the
read-only `account/rateLimits/read`. It never refreshes a credential: a login
whose access token is about to expire is skipped. If the client rotates the
login anyway, the rotated document is written back so the stored one stays
valid. Output is parsed, never logged.
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx2 as httpx
from argus_core import claude_auth, codex_auth, cursor_auth
from argus_core.accounts import QuotaRefresh, QuotaReport, QuotaStatusReason
from argus_core.custom_accounts import PROVIDER as CUSTOM_PROVIDER
from argus_core.custom_accounts import CustomConfig

from argus_api.accounts import quota_readers, usage
from argus_api.config import Settings
from argus_api.db import Database, ts, utcnow
from argus_api.events import Event, OverflowedError, record
from argus_api.ops import LoopStats
from argus_api.secrets import SecretStore, SecretStoreError, ref_name

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 15.0
MIN_INTERVAL_S = 300.0  # per account
MIN_TOKEN_LIFE = timedelta(minutes=10)

# Inventory and staleness refresh (account usage 3.3): bounded, never a storm.
STARTUP_DELAY_S = 10.0  # first sweep, after the app is up
SWEEP_INTERVAL_S = 900.0  # then every 15 minutes
REFRESH_AFTER = timedelta(minutes=30)  # a snapshot (or probe) older than this is due
PROBE_GAP_S = 2.0  # pause after each probe that ran
SWEEP_CAP = 10  # accounts queued per sweep


class Skip(Exception):
    """Not probed, for a reason worth showing (no credential value in it). `code`
    is the `QuotaStatusReason` it falls under."""

    def __init__(self, code: QuotaStatusReason, message: str) -> None:
        super().__init__(message)
        self.code = code


Status = Literal["updated", "skipped", "rate_limited", "failed"]


@dataclass
class Outcome:
    status: Status
    reason: str | None = None
    code: QuotaStatusReason | None = None


@dataclass
class Reading:
    windows: list[QuotaReport] = field(default_factory=list)
    credit: usage.CreditReading | None = None
    rotated: bool = False
    note: str | None = None  # shown with an `updated` outcome (e.g. a locked window)


class QuotaProber:
    def __init__(
        self,
        db: Database,
        secrets: SecretStore,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport  # tests only: the provider reads go through it
        self._db, self._secrets, self._settings = db, secrets, settings
        self._clock = clock  # monotonic seconds: the per-account floor
        self._now = now  # wall time: how old a snapshot is
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._lock = asyncio.Lock()  # one client process at a time

    # -- public ------------------------------------------------------------------

    def request(self, account_id: str) -> None:
        """Queue a background probe; a queued account is not queued twice."""
        if account_id not in self._queued:
            self._queued.add(account_id)
            self._queue.put_nowait(account_id)

    async def refresh(self, account_id: str) -> QuotaRefresh:
        """Probe now (the dashboard's "Refresh quota"), under the same limits."""
        outcome = await self.probe(account_id)
        async with self._db.read() as conn:
            quota = await usage.list_quota(conn, account_id)
            row = await _account(conn, account_id)
            credit = (
                await usage.get_credit(conn, account_id, row["provider"])
                if row
                else None
            )
            status = (
                await usage.quota_status(conn, row, quota, self._now()) if row else None
            )
        return QuotaRefresh(
            status=outcome.status,
            code=outcome.code,
            reason=outcome.reason,
            quota=quota,
            credit=credit,
            quota_status=status,
        )

    async def probe(self, account_id: str) -> Outcome:
        async with self._lock:
            last = self._last.get(account_id)
            if last is not None and self._clock() - last < MIN_INTERVAL_S:
                outcome = Outcome("rate_limited", "probed less than 5 minutes ago")
                # The probe inside the floor already stored its own outcome.
                await self._store(account_id, outcome, keep=True)
                return outcome
            self._last[account_id] = self._clock()
            reading = Reading()
            try:
                reading = await self._read(account_id)
                outcome = (
                    Outcome("updated", reading.note)
                    if reading.windows or reading.credit
                    else Outcome("skipped", "no windows", "no_windows")
                )
            except Skip as exc:
                outcome = Outcome("skipped", str(exc), exc.code)
            except quota_readers.ReadError as exc:
                outcome = Outcome("failed", str(exc), exc.code)
            except Exception as exc:  # the client, its protocol, the filesystem
                log.warning(
                    "quota probe of %s failed: %s", account_id, type(exc).__name__
                )
                outcome = Outcome("failed", type(exc).__name__, "probe_failed")
        await self._store(account_id, outcome, reading)
        return outcome

    async def _store(
        self,
        account_id: str,
        outcome: Outcome,
        reading: Reading | None = None,
        keep: bool = False,
    ) -> None:
        """Outside the probe, in one short write: the figures and the outcome. A
        probe that did not read leaves the last good figures as they were."""
        async with self._db.write() as tx:
            if reading and reading.rotated:
                await record(
                    tx, "system", "account", account_id, "account.credential_rotated"
                )
            if reading and reading.windows:
                await usage.upsert_quota(
                    tx, account_id, reading.windows, "polled", "system"
                )
            if reading and reading.credit:
                await usage.upsert_credit(tx, account_id, reading.credit, "system")
            await usage.record_probe(
                tx,
                account_id,
                outcome.status,
                outcome.reason,
                code=outcome.code,
                keep=keep,
            )

    async def sweep(self, *, backoff: bool = True) -> list[str]:
        """Queue the accounts with a pollable source (Codex, Claude subscription,
        OpenRouter) whose quota is missing or stale: at most
        `SWEEP_CAP`, the longest-unobserved first. A recent probe (any outcome)
        defers an account, unless `backoff` is off (the start-up inventory)."""
        cutoff = ts(self._now() - REFRESH_AFTER)
        async with (
            self._db.read() as conn,
            conn.execute(
                "SELECT a.id, a.provider, a.kind, a.config,"
                " (SELECT max(t) FROM (SELECT max(observed_at) AS t FROM account_quota q"
                "  WHERE q.account_id = a.id UNION ALL SELECT observed_at FROM"
                "  account_credit c WHERE c.account_id = a.id)) AS seen,"
                " (SELECT at FROM account_quota_probe p"
                "  WHERE p.account_id = a.id) AS probed"
                " FROM accounts a WHERE a.state != 'disabled'"
            ) as cur,
        ):
            rows = await cur.fetchall()
        due = [
            r
            for r in rows
            if _pollable(r)
            and (r["seen"] is None or r["seen"] <= cutoff)
            and (not backoff or r["probed"] is None or r["probed"] <= cutoff)
        ]
        due.sort(key=lambda r: (r["seen"] or "", r["id"]))
        queued = []
        for r in due[:SWEEP_CAP]:
            if r["id"] not in self._queued:
                self.request(r["id"])
                queued.append(r["id"])
        return queued

    async def _sweeper(self) -> None:
        await self._sleep(STARTUP_DELAY_S)
        backoff = False  # the inventory ignores earlier outcomes, later sweeps do not
        while True:
            try:
                await self.sweep(backoff=backoff)
            except Exception:  # a failed sweep must not end the loop
                log.exception("quota sweep failed")
            backoff = True
            await self._sleep(SWEEP_INTERVAL_S)

    async def run(self, stats: LoopStats | None = None) -> None:
        """Queue probes when runs end and as the inventory ages, and work the queue
        one account at a time (account usage 3.3)."""
        watcher = asyncio.create_task(self._watch())
        sweeper = asyncio.create_task(self._sweeper())
        try:
            while True:
                account_id = await self._queue.get()
                self._queued.discard(account_id)
                started = time.monotonic()
                outcome = await self.probe(account_id)
                if stats:
                    stats.record(time.monotonic() - started, outcome.status != "failed")
                if outcome.status != "rate_limited":
                    await self._sleep(PROBE_GAP_S)
        finally:
            for task in (watcher, sweeper):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # -- triggers ------------------------------------------------------------------

    async def _watch(self) -> None:
        while True:
            with self._db.bus.subscribe() as sub:
                try:
                    while True:
                        event = await sub.next_matching(_run_ended)
                        for account_id in await self._stale_accounts(event):
                            self.request(account_id)
                except OverflowedError:
                    continue  # missed some: later runs will queue their own

    async def _stale_accounts(self, event: Event) -> list[str]:
        """Pollable accounts the run's agent held with no snapshot since the claim."""
        if event.subject_type == "account":  # exhaustion reported
            candidates = [event.subject_id]
            since = None
        else:
            if not event.actor.startswith("agent:"):
                return []
            agent_id = event.actor.removeprefix("agent:")
            async with self._db.read() as conn:
                async with conn.execute(
                    "SELECT DISTINCT account_id FROM account_leases WHERE agent_id = ?"
                    " AND leased_at <= ? AND (released_at IS NULL OR released_at >= ?)",
                    (agent_id, ts(event.at), ts(event.at)),
                ) as cur:
                    candidates = [r[0] for r in await cur.fetchall()]
                async with conn.execute(
                    "SELECT max(at) FROM events WHERE subject_type = 'task'"
                    " AND subject_id = ? AND kind = 'task.claim' AND id < ?",
                    (event.subject_id, event.id),
                ) as cur:
                    row = await cur.fetchone()
                since = datetime.fromisoformat(row[0]) if row and row[0] else None
        stale = []
        async with self._db.read() as conn:
            for account_id in candidates:
                row = await _account(conn, account_id)
                if row is None or not _pollable(row):
                    continue
                seen = await usage.last_observed(conn, account_id)
                if seen is None or since is None or seen < since:
                    stale.append(account_id)
        return stale

    # -- the probe -----------------------------------------------------------------

    async def _read(self, account_id: str) -> Reading:
        async with self._db.read() as conn:
            row = await _account(conn, account_id)
        if row is None:
            raise Skip("no_provider_source", "account not found")
        config = json.loads(row["config"]) if row["config"] else None
        source = usage.quota_source(row["provider"], row["kind"], config)
        if source is None:
            code = usage.no_source_reason(row["provider"], row["kind"])
            raise Skip(code, _NO_SOURCE[code])
        name = ref_name(row["secret_ref"])
        doc = self._secrets.get(name) if name else None
        if not isinstance(doc, dict):
            raise Skip("credential_missing", "credential not found")
        if source == "codex":
            return await self._read_codex(name, doc)
        if source == "claude":
            return await self._read_claude(doc)
        if source == "cursor":
            return await self._read_cursor(doc)
        return await self._read_openrouter(CustomConfig.model_validate(config), doc)

    async def _read_claude(self, doc: dict[str, Any]) -> Reading:
        """Claude's OAuth usage read with a stored login. Never refreshes the login:
        an expired access token is reported, and an API key has no usage source."""
        try:
            summary = claude_auth.validate(doc, now=_EPOCH)
        except claude_auth.ClaudeAuthError as exc:
            raise Skip(
                "credential_invalid", "credential is not a Claude login"
            ) from exc
        if summary.mode == "api_key":
            raise Skip("api_key_account", _NO_SOURCE["api_key_account"])
        if summary.mode == "oauth_token":
            token = doc["oauth_token"]
        else:
            expires = summary.access_expires_at
            if expires is not None and expires <= utcnow():
                raise Skip(
                    "credential_expired",
                    "access token expired; the holder refreshes it, Argus does not",
                )
            token = doc["claudeAiOauth"]["accessToken"]
        usage_ = await quota_readers.read_claude(token, self._transport)
        note = (
            f"locked, no figure: {', '.join(usage_.locked)}" if usage_.locked else None
        )
        return Reading(windows=usage_.windows, note=note)

    async def _read_cursor(self, doc: dict[str, Any]) -> Reading:
        """Cursor's dashboard calls with an imported login's access token. Never
        refreshes the login; a JWT past its `exp` is reported."""
        try:
            summary = cursor_auth.validate(doc, now=_EPOCH)
        except cursor_auth.CursorAuthError as exc:
            raise Skip(
                "credential_invalid", "credential is not a Cursor login"
            ) from exc
        if summary.mode != "oauth":
            raise Skip("admin_key_required", _NO_SOURCE["admin_key_required"])
        expires = summary.access_expires_at
        if expires is not None and expires <= utcnow():
            raise Skip(
                "credential_expired",
                "access token expired; the holder refreshes it, Argus does not",
            )
        read = await quota_readers.read_cursor(doc["accessToken"], self._transport)
        if read.credit is None:
            detail = f" ({read.note})" if read.note else ""
            raise Skip(
                "no_usage_window_for_login",
                f"the login answered but states no numeric usage or credit{detail}",
            )
        return Reading(credit=read.credit, note=read.note or None)

    async def _read_openrouter(
        self, config: CustomConfig, doc: dict[str, Any]
    ) -> Reading:
        key = doc.get("api_key")
        if not isinstance(key, str) or not key:
            raise Skip("credential_invalid", "credential has no api_key")
        credit = await quota_readers.read_openrouter(
            config.base_url, key, self._transport
        )
        return Reading(credit=credit)

    async def _read_codex(self, name: str | None, doc: dict[str, Any]) -> Reading:
        if not self._settings.codex_bin:
            raise Skip(
                "probe_disabled", "codex binary not configured (ARGUS_CODEX_BIN)"
            )
        try:
            summary = codex_auth.validate(doc)
        except codex_auth.CodexAuthError as exc:
            raise Skip("credential_invalid", "credential is not a Codex login") from exc
        if summary.mode != "chatgpt":
            raise Skip("api_key_account", _NO_SOURCE["api_key_account"])
        expires = summary.access_expires_at
        if expires is None or expires - utcnow() < MIN_TOKEN_LIFE:
            raise Skip(
                "credential_expired",
                "access token expires soon; the holder will refresh it",
            )

        home = _stage(doc)
        try:
            result = await asyncio.wait_for(self._ask(home), PROBE_TIMEOUT_S)
            after = _read_back(home)
            rotated = _rotated(doc, after)
            if rotated:
                assert name is not None
                await self._keep_rotated(name, after)
            return Reading(windows=_windows(result), rotated=rotated)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    async def _keep_rotated(self, name: str, doc: dict[str, Any]) -> None:
        """The client rotated the login: the old refresh token is now spent, so the
        stored credential must become the new one."""
        try:
            await self._secrets.put(name, doc)
        except SecretStoreError:
            log.error("quota probe rotated a Codex login and could not store it")
            raise

    async def _ask(self, home: Path) -> dict[str, Any]:
        assert self._settings.codex_bin
        proc = await asyncio.create_subprocess_exec(
            self._settings.codex_bin,
            "app-server",
            "-c",
            'cli_auth_credentials_store="file"',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "CODEX_HOME": str(home),
                "HOME": str(home),
            },
            start_new_session=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        stdin, stdout = proc.stdin, proc.stdout

        async def send(msg: dict[str, Any]) -> None:
            stdin.write((json.dumps(msg) + "\n").encode())
            await stdin.drain()

        try:
            await send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "argus",
                            "title": "Argus",
                            "version": "1",
                        }
                    },
                }
            )
            while line := await stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError("initialize")
                    await send({"method": "initialized"})
                    await send(
                        {
                            "id": 2,
                            "method": "account/rateLimits/read",
                            "params": {"excludeResetCreditDetails": True},
                        }
                    )
                elif msg.get("id") == 2:
                    result = msg.get("result")
                    if not isinstance(result, dict):
                        raise RuntimeError("rate_limits_error")
                    return result
            raise RuntimeError("client_exited")
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)  # validate() without its expiry check
_NO_SOURCE: dict[QuotaStatusReason, str] = {
    "api_key_account": "an API key has no usage window to read; only a login does",
    "admin_key_required": (
        "a Cursor API key reads usage only through the team Admin API (an admin "
        "key); import a Cursor login to read it from the account itself"
    ),
    "local_no_quota": "the node's own model has no provider quota",
    "no_provider_source": "no read-only quota source exists for this account",
}


async def _account(conn, account_id: str) -> dict | None:
    async with conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


def _pollable(row) -> bool:
    config = json.loads(row["config"]) if row["config"] else None
    if row["kind"] != "subscription" and row["provider"] != CUSTOM_PROVIDER:
        return False  # an API key holds no login to read
    return usage.quota_source(row["provider"], row["kind"], config) is not None


def _stage(doc: dict[str, Any]) -> Path:
    """A private CODEX_HOME holding only this login, marked freshly refreshed so
    the client has no reason to rotate it."""
    home = Path(tempfile.mkdtemp(prefix="argus-codex-quota-"))
    home.chmod(0o700)
    auth = home / "auth.json"
    auth.write_text(json.dumps({**doc, "last_refresh": utcnow().isoformat()}))
    auth.chmod(0o600)
    return home


def _read_back(home: Path) -> dict[str, Any]:
    doc = json.loads((home / "auth.json").read_text())
    return doc if isinstance(doc, dict) else {}


def _run_ended(event: Event) -> bool:
    """A run left `claimed` (completed, failed, blocked, asked, expired), or an
    account reported exhaustion."""
    if event.subject_type == "account":
        return event.kind == "account.exhausted"
    return event.subject_type == "task" and event.data.get("from") == "claimed"


def _refresh_token(doc: dict[str, Any]) -> Any:
    tokens = doc.get("tokens")
    return tokens.get("refresh_token") if isinstance(tokens, dict) else None


def _rotated(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Only a valid login with a new refresh token counts: anything else (an
    unreadable or partial file) must never replace the stored credential."""
    try:
        codex_auth.validate(after)
    except codex_auth.CodexAuthError:
        return False
    return _refresh_token(after) != _refresh_token(before)


def _windows(result: dict[str, Any]) -> list[QuotaReport]:
    """`rateLimits.primary` and `.secondary` as timed windows."""
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        return []
    out = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if not isinstance(w, dict):
            continue
        minutes, used, resets = (
            w.get("windowDurationMins"),
            w.get("usedPercent"),
            w.get("resetsAt"),
        )
        if not isinstance(minutes, int) or minutes <= 0:
            continue
        if not isinstance(used, int | float) or isinstance(used, bool):
            continue
        out.append(
            QuotaReport(
                window_minutes=minutes,
                used_percent=min(max(float(used), 0.0), 100.0),
                resets_at=datetime.fromtimestamp(resets, UTC)
                if isinstance(resets, int) and not isinstance(resets, bool)
                else None,
            )
        )
    return out
