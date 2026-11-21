"""Control-plane refresh for Google (Antigravity) OAuth credentials
(docs/specs/google-accounts.spec.md).

Google OAuth access tokens expire after 1 hour (`expires_in: 3600`). Before
`POST /api/accounts/lease` opens a write transaction for an executor lease that
may select a `google` account, this module refreshes any OAuth credential whose
`expiry` is within `REFRESH_MARGIN` (10m) and persists the updated document back
into SOPS. Never runs inside a write transaction and never echoes secret values.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from argus_core import google_auth

from argus_api.accounts import clients
from argus_api.accounts.eligibility import LEASE_BLOCK_SQL
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.events import record
from argus_api.secrets import SecretStore, ref_name

log = logging.getLogger(__name__)

GOOGLE_OAUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
REFRESH_MARGIN = google_auth.REFRESH_MARGIN
MIN_FRESH_MARGIN = timedelta(minutes=5)
HTTP_TIMEOUT_S = 15.0
CLI_TIMEOUT_S = 20.0
_PROBE_MODEL = "__argus_refresh_probe__"
_SCRUB_ENV = (
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "GNOME_KEYRING_CONTROL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ARGUS_AGY_OAUTH_STUB",
)
_PERMANENT_OAUTH_ERRORS = frozenset({"invalid_grant", "unauthorized_client"})
# Mid-lease refresh is one provider attempt per account per window: a failed or
# forced attempt makes later calls inside it answer from the last outcome, so a
# worker that keeps hitting 401 cannot drive unbounded token-endpoint traffic.
ATTEMPT_COOLDOWN = timedelta(seconds=60)

RefreshOutcome = Literal["current", "refreshed", "transient", "permanent", "throttled"]
"""`current`: stored token not near expiry (nothing done). `refreshed`: persisted
a new one. `transient`/`permanent`: provider refresh failed (permanent also
records `account.auth_rejected`). `throttled`: a recent attempt's outcome stands."""

PostForm = Callable[[str, dict[str, str]], Awaitable[tuple[int, dict[str, Any]]]]


class GoogleRefreshError(RuntimeError):
    """Refresh failed. Message names the cause only, never any credential value."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _non_empty_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _client_credentials(doc: dict[str, Any]) -> tuple[str, str] | None:
    """Resolve optional OAuth client_id and client_secret from doc or env."""
    raw_token = doc.get("token")
    inner: dict[str, Any] = raw_token if isinstance(raw_token, dict) else {}
    client_id = (
        _non_empty_str(doc.get("client_id"))
        or _non_empty_str(inner.get("client_id"))
        or _non_empty_str(os.environ.get("ARGUS_GOOGLE_OAUTH_CLIENT_ID"))
    )
    client_secret = (
        _non_empty_str(doc.get("client_secret"))
        or _non_empty_str(inner.get("client_secret"))
        or _non_empty_str(os.environ.get("ARGUS_GOOGLE_OAUTH_CLIENT_SECRET"))
    )
    if client_id and client_secret:
        return client_id, client_secret
    return None


def resolve_agy_bin(explicit: str | None = None) -> str | None:
    """Locate an executable `agy` CLI on the control-plane host, if installed."""
    candidates = [
        explicit,
        os.environ.get("ARGUS_AGY_BIN"),
        shutil.which("agy"),
        str(Path.home() / ".local" / "bin" / "agy"),
    ]
    for raw in candidates:
        path = _non_empty_str(raw)
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _post_form_sync(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    if not url.startswith("https://"):
        raise GoogleRefreshError("google OAuth endpoint must use https")
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GoogleRefreshError(
            "google OAuth token endpoint request failed (network error)"
        ) from None
    try:
        parsed = json.loads(raw.decode("utf-8", errors="ignore"))
        payload = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        payload = {}
    return status, payload


async def _default_post_form(
    url: str, form: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    return await asyncio.to_thread(_post_form_sync, url, form)


async def _refresh_via_http(
    doc: dict[str, Any],
    *,
    client_id: str,
    client_secret: str,
    now: datetime,
    post_form: PostForm | None = None,
) -> dict[str, Any]:
    rt = google_auth.refresh_token(doc)
    if not rt:
        raise GoogleRefreshError("google OAuth credential has no refresh_token")
    poster = post_form or _default_post_form
    status, payload = await poster(
        GOOGLE_OAUTH_ENDPOINT,
        {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )

    if not (200 <= status < 300):
        err_code = str(payload.get("error") or "").strip().lower()
        permanent = err_code in _PERMANENT_OAUTH_ERRORS
        detail = err_code or f"HTTP {status}"
        raise GoogleRefreshError(
            f"google OAuth refresh rejected ({detail})", permanent=permanent
        )

    access = _non_empty_str(payload.get("access_token"))
    if not access or any(c.isspace() or not c.isprintable() for c in access):
        raise GoogleRefreshError("google OAuth refresh returned no usable access_token")
    raw_expires = payload.get("expires_in")
    expires_in = (
        int(raw_expires)
        if isinstance(raw_expires, int | float)
        and not isinstance(raw_expires, bool)
        and raw_expires > 0
        else 3600
    )
    expiry = now + timedelta(seconds=expires_in)
    token_type = _non_empty_str(payload.get("token_type"))
    rotated_rt = _non_empty_str(payload.get("refresh_token"))
    merged = google_auth.merge_refreshed_token(
        doc,
        access_token=access,
        expiry=expiry,
        expires_in=expires_in,
        token_type=token_type,
        new_refresh_token=rotated_rt,
    )
    google_auth.validate(merged, now=now)
    return merged


def _staged_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Native `antigravity-oauth-token` shape with past expiry to force CLI refresh."""
    inner = doc.get("token")
    fields = inner if isinstance(inner, dict) else doc
    payload: dict[str, Any] = {
        **fields,
        "expiry": "1970-01-01T00:00:00Z",
    }
    payload.setdefault("token_type", "Bearer")
    payload.setdefault("expires_in", 3600)
    staged: dict[str, Any] = {
        "token": payload,
        "auth_method": doc.get("auth_method") or "consumer",
    }
    if "id_token_email" in doc:
        staged["id_token_email"] = doc["id_token_email"]
    return staged


def _stage_temp_home(doc: dict[str, Any]) -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="argus-google-refresh-"))
    tmp.chmod(0o700)
    cli_dir = tmp / ".gemini" / "antigravity-cli"
    cli_dir.mkdir(parents=True, mode=0o700)
    dest = cli_dir / "antigravity-oauth-token"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(_staged_doc(doc), fh)
    return tmp, dest


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text())
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None


async def _refresh_via_agy(
    doc: dict[str, Any],
    *,
    agy_bin: str,
    now: datetime,
) -> dict[str, Any]:
    """Stage the credential in an isolated 0700 HOME with D-Bus/keyring scrubbed,
    let `agy` refresh the token before failing on an unknown model name, and read
    the refreshed token back."""
    prior_access = google_auth.extract_oauth_fields(doc).get("access_token")
    tmp, token_file = await asyncio.to_thread(_stage_temp_home, doc)
    try:
        env = {k: v for k, v in os.environ.items() if k not in _SCRUB_ENV}
        env["HOME"] = str(tmp)
        env["XDG_CONFIG_HOME"] = str(tmp / ".config")
        env["XDG_DATA_HOME"] = str(tmp / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(tmp / ".cache")
        env["NO_BROWSER"] = "1"

        proc = await asyncio.create_subprocess_exec(
            agy_bin,
            "--output-format",
            "stream-json",
            "--model",
            _PROBE_MODEL,
            "-p",
            ".",
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=CLI_TIMEOUT_S
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise GoogleRefreshError("agy OAuth refresh timed out") from None

        updated_raw = await asyncio.to_thread(_read_json_file, token_file)

        if isinstance(updated_raw, dict):
            try:
                updated_fields = google_auth.extract_oauth_fields(updated_raw)
                new_access = _non_empty_str(updated_fields.get("access_token"))
                new_expiry = google_auth.access_expiry(updated_raw)
            except google_auth.GoogleAuthError:
                new_access = None
                new_expiry = None
            if (
                new_access
                and new_expiry is not None
                and new_expiry > now + MIN_FRESH_MARGIN
                and (new_access != prior_access or new_expiry > now + REFRESH_MARGIN)
            ):
                raw_exp_in = updated_fields.get("expires_in")
                expires_in = (
                    int(raw_exp_in)
                    if isinstance(raw_exp_in, int | float)
                    and not isinstance(raw_exp_in, bool)
                    and raw_exp_in > 0
                    else None
                )
                merged = google_auth.merge_refreshed_token(
                    doc,
                    access_token=new_access,
                    expiry=new_expiry,
                    expires_in=expires_in,
                    token_type=_non_empty_str(updated_fields.get("token_type")),
                    new_refresh_token=_non_empty_str(
                        updated_fields.get("refresh_token")
                    ),
                )
                google_auth.validate(merged, now=now)
                return merged

        combined = (
            stdout_b.decode("utf-8", errors="ignore")
            + "\n"
            + stderr_b.decode("utf-8", errors="ignore")
        ).lower()
        if (
            "invalid_grant" in combined
            or "token has been expired or revoked" in combined
        ):
            raise GoogleRefreshError(
                "google OAuth refresh token rejected (invalid_grant)", permanent=True
            )
        raise GoogleRefreshError("agy did not refresh the google OAuth access_token")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def refresh_oauth_credential(
    doc: dict[str, Any],
    *,
    now: datetime | None = None,
    post_form: PostForm | None = None,
    agy_bin: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Refresh an expiring Google OAuth credential via direct HTTP (when client
    credentials are configured), an installed `agy` binary, or the pinned `agy`
    client downloaded on first use via `accounts/clients.py`."""
    current = now or datetime.now(UTC)
    creds = _client_credentials(doc)
    if creds is not None:
        return await _refresh_via_http(
            doc,
            client_id=creds[0],
            client_secret=creds[1],
            now=current,
            post_form=post_form,
        )
    resolved = resolve_agy_bin(agy_bin)
    if resolved is None and settings is not None:
        try:
            resolved = str(await clients.ensure(settings, "agy"))
        except clients.ClientDownloadError as exc:
            raise GoogleRefreshError(
                f"cannot download pinned agy client for OAuth refresh: {exc}"
            ) from None
    if resolved is not None:
        return await _refresh_via_agy(doc, agy_bin=resolved, now=current)
    raise GoogleRefreshError(
        "cannot refresh google OAuth token: neither ARGUS_GOOGLE_OAUTH_CLIENT_ID/"
        "SECRET nor an `agy` binary is available on the API host"
    )


def _expired_without_refresh_token(doc: dict[str, Any], now: datetime) -> bool:
    """An access-only OAuth credential past its expiry cannot be renewed."""
    if not google_auth.is_oauth(doc) or google_auth.refresh_token(doc) is not None:
        return False
    exp = google_auth.access_expiry(doc)
    return exp is not None and exp <= now


def _access_expired(doc: dict[str, Any], now: datetime) -> bool:
    """The stored OAuth access token is at or past its expiry."""
    if not google_auth.is_oauth(doc):
        return False
    exp = google_auth.access_expiry(doc)
    return exp is not None and exp <= now


def _access_token(doc: dict[str, Any]) -> str | None:
    """The stored access token, kept only in process memory to recognise a
    rejected one; never logged or returned."""
    try:
        token = google_auth.extract_oauth_fields(doc).get("access_token")
    except google_auth.GoogleAuthError:
        return None
    if not isinstance(token, str):
        return None
    return token


class GoogleTokenRefresher:
    """Serialized per-account OAuth refresher attached to `app.state`."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        post_form: PostForm | None = None,
        agy_bin: str | None = None,
    ) -> None:
        self.settings = settings
        self.post_form = post_form
        self.agy_bin = agy_bin
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_attempt: dict[str, tuple[datetime, RefreshOutcome]] = {}
        # The access token whose refresh failed. The token stays
        # invalid until a refresh or a credential replacement changes it,
        # independently of the attempt cooldown.
        self._invalid: dict[str, str | None] = {}

    def _is_invalid(self, account_id: str, doc: dict[str, Any]) -> bool:
        rejected = self._invalid.get(account_id)
        return rejected is not None and rejected == _access_token(doc)

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        lock = self._locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_id] = lock
        return lock

    async def _refresh_row(
        self,
        db: Database,
        secrets: SecretStore,
        account_id: str,
        account_name: str,
        secret_ref: str | None,
        *,
        now: datetime,
        force: bool = False,
    ) -> RefreshOutcome:
        """Refresh one account's credential. `force` refreshes even when the stored
        expiry is not near (the executor saw a 401 on a token Argus thinks is
        fresh), still at most once per `ATTEMPT_COOLDOWN`."""
        secret_name = ref_name(secret_ref) if secret_ref else None
        if not secret_name:
            return "current"
        doc = secrets.get(secret_name)
        if not isinstance(doc, dict):
            return "current"
        last = self._last_attempt.get(account_id)
        if (
            last is not None
            and now - last[0] < ATTEMPT_COOLDOWN
            and last[1] in {"transient", "permanent"}
        ):
            # A failed attempt stands for the window even when the stored token
            # still looks fresh (e.g. after a failed forced refresh).
            return last[1]
        invalid = self._is_invalid(account_id, doc)
        if not (force or invalid) and not google_auth.needs_refresh(doc, now=now):
            if _expired_without_refresh_token(doc, now):
                return "permanent"
            return "current"
        async with self._lock_for(account_id):
            latest = secrets.get(secret_name)
            if not isinstance(latest, dict):
                return "current"
            last = self._last_attempt.get(account_id)
            if last is not None and now - last[0] < ATTEMPT_COOLDOWN:
                # Inside the window the last outcome stands: a failure is not
                # retried, and a forced call after a success marks the token
                # rejected without contacting the provider again.
                if last[1] in {"transient", "permanent"}:
                    return last[1]
                if force:
                    self._invalid[account_id] = _access_token(latest)
                    return "throttled"
                if self._is_invalid(account_id, latest) or _access_expired(latest, now):
                    # Never hand back a token past its expiry (e.g. an
                    # `expires_in` shorter than the cooldown).
                    return "throttled"
                return "current"
            invalid = self._is_invalid(account_id, latest)
            if not (force or invalid) and not google_auth.needs_refresh(
                latest, now=now
            ):
                return "current"
            if google_auth.refresh_token(latest) is None:
                if force or invalid or _expired_without_refresh_token(latest, now):
                    self._invalid[account_id] = _access_token(latest)
                    return "permanent"
                return "current"
            # Mark the token before awaiting the provider: a plain call that
            # overlaps this refresh skips the lock-free fast path and waits for
            # the outcome instead of returning the token being refreshed.
            self._invalid[account_id] = _access_token(latest)
            try:
                refreshed = await refresh_oauth_credential(
                    latest,
                    now=now,
                    post_form=self.post_form,
                    agy_bin=self.agy_bin,
                    settings=self.settings,
                )
            except GoogleRefreshError as exc:
                log.warning(
                    "Google OAuth refresh failed for account %s (%s): %s",
                    account_name,
                    account_id,
                    exc,
                )
                outcome: RefreshOutcome = "permanent" if exc.permanent else "transient"
                self._last_attempt[account_id] = (now, outcome)
                self._invalid[account_id] = _access_token(latest)
                if exc.permanent:
                    async with db.write() as tx:
                        await record(
                            tx,
                            "system",
                            "account",
                            account_id,
                            "account.auth_rejected",
                            {
                                "role": "executor",
                                "source": "oauth_refresh",
                                "reason": "Google OAuth refresh token rejected; renew the account in Argus",
                            },
                        )
                return outcome
            await secrets.put(secret_name, refreshed)
            self._last_attempt[account_id] = (now, "refreshed")
            self._invalid.pop(account_id, None)
            log.info(
                "Refreshed Google OAuth access_token for account %s (%s)",
                account_name,
                account_id,
            )
            return "refreshed"

    async def refresh_for_lease(
        self,
        db: Database,
        secrets: SecretStore,
        *,
        now: datetime | None = None,
    ) -> None:
        """Refresh any usable Google OAuth accounts nearing expiry before leasing
        or claiming an execution chain.

        Runs outside any write transaction. Permanent refresh failures record
        `account.auth_rejected` with a fixed non-secret reason so the account is
        not repeatedly leased; transient errors log and leave it eligible.
        """
        current = now or datetime.now(UTC)
        async with (
            db.read() as conn,
            conn.execute(
                "SELECT a.id, a.name, a.secret_ref FROM accounts a"  # noqa: S608
                " WHERE a.provider = 'google'"
                " AND a.state IN ('available', 'exhausted')"
                f" AND {LEASE_BLOCK_SQL} IS NULL"
            ) as cur,
        ):
            rows = await cur.fetchall()

        for row in rows:
            await self._refresh_row(
                db,
                secrets,
                str(row["id"]),
                str(row["name"]),
                row["secret_ref"],
                now=current,
            )

    async def refresh_account(
        self,
        db: Database,
        secrets: SecretStore,
        account_id: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> RefreshOutcome:
        """Refresh one Google OAuth account if nearing expiry (e.g. after a
        long-poll claim wait, right before delivering an execution-chain lease,
        or mid-lease when the executor asks for a current credential)."""
        current = now or datetime.now(UTC)
        async with (
            db.read() as conn,
            conn.execute(
                "SELECT id, name, secret_ref FROM accounts"
                " WHERE id = ? AND provider = 'google'",
                (account_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None:
            return "current"
        return await self._refresh_row(
            db,
            secrets,
            str(row["id"]),
            str(row["name"]),
            row["secret_ref"],
            now=current,
            force=force,
        )


def refresher_for(state: Any) -> GoogleTokenRefresher:
    """Return the app's shared GoogleTokenRefresher, creating one if absent."""
    refresher = getattr(state, "google_refresher", None)
    if refresher is None:
        refresher = GoogleTokenRefresher(settings=getattr(state, "settings", None))
        state.google_refresher = refresher
    return refresher
