"""Shape of a Google (Antigravity) account credential (google accounts spec 3).
Validation never echoes a value: errors name fields and rules only, never a key
or token."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

PROVIDER = "google"
MAX_TOKEN_LEN = 8192
REFRESH_MARGIN = timedelta(minutes=10)
_API_KEY_ONLY = frozenset({"api_key"})


class GoogleAuthError(ValueError):
    """The document is not a usable Google/Antigravity credential. Names fields only."""


class GoogleAuthSummary(BaseModel):
    """What may be shown about a stored credential: no key, no token, no email."""

    mode: str  # "api_key" | "oauth"
    kind: str  # Argus AccountKind value ("api_key" | "subscription")
    access_expires_at: datetime | None = None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _token(doc: dict[str, Any], field: str) -> str:
    value = doc.get(field)
    if not _text(value):
        raise GoogleAuthError(f"{field} must be a non-empty string")
    assert isinstance(value, str)
    if len(value) > MAX_TOKEN_LEN:
        raise GoogleAuthError(f"{field} is too long")
    if any(c.isspace() or not c.isprintable() for c in value):
        raise GoogleAuthError(f"{field} must not contain whitespace")
    return value


def parse_expiry(raw: Any) -> datetime:
    """Parse an RFC 3339 / ISO-8601 expiry timestamp into a timezone-aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        raise GoogleAuthError("expiry must be an ISO-8601 timestamp string")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise GoogleAuthError("expiry must be a valid ISO-8601 timestamp") from None
    if dt.tzinfo is None:
        raise GoogleAuthError("expiry must include a timezone offset")
    return dt.astimezone(UTC)


def extract_oauth_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the token dict from either native `{"token": {...}}` or flat form.

    Extra CLI metadata fields (such as `id_token_email`, `auth_method`, `scope`)
    are ignored, matching Codex and Cursor OAuth imports.
    """
    if "token" in doc:
        inner = doc.get("token")
        if not isinstance(inner, dict):
            raise GoogleAuthError("token must be a JSON object")
        return inner
    return doc


def validate(doc: dict[str, Any], *, now: datetime | None = None) -> GoogleAuthSummary:
    """Two mutually exclusive shapes, never mixed:

    - `{"api_key": …}`: a Gemini API key (kind `api_key`), delivered via OneCLI
      `x-goog-api-key` injection for `agy --settings '{"modelProvider":"gemini"}'`.
    - Native `antigravity-oauth-token` (`{"token": {"access_token": …, …}, …}`) or
      flat `{"access_token": …, …}`: an imported Google OAuth subscription login
      (kind `subscription`), delivered via OneCLI Bearer injection and a sentinel
      `antigravity-oauth-token` stub.
    """
    has_api_key = "api_key" in doc
    has_oauth = "token" in doc or "access_token" in doc
    if not has_api_key and not has_oauth:
        raise GoogleAuthError(
            "needs one of: api_key, or an OAuth login document with access_token "
            "(a settings.json file is not a credential)"
        )
    if has_api_key and has_oauth:
        raise GoogleAuthError("give only one of: api_key or an OAuth login document")

    if has_api_key:
        extra = sorted(set(doc) - _API_KEY_ONLY)
        if extra:
            raise GoogleAuthError(
                f"only the field api_key is allowed; remove {extra[0]}"
            )
        value = _token(doc, "api_key")
        if value.startswith(("ya29.", "1//")):
            raise GoogleAuthError(
                "api_key holds a Google OAuth token; import it as an OAuth login "
                "document with access_token instead"
            )
        return GoogleAuthSummary(mode="api_key", kind="api_key")

    if "token" in doc and "access_token" in doc:
        raise GoogleAuthError(
            "give either nested token.access_token or flat access_token, not both"
        )
    fields = extract_oauth_fields(doc)
    access = _token(fields, "access_token")
    if access.startswith("AIza"):
        raise GoogleAuthError(
            "access_token holds a Gemini API key; put it in api_key instead"
        )
    has_refresh = False
    if "refresh_token" in fields and fields["refresh_token"] is not None:
        _token(fields, "refresh_token")
        has_refresh = True
    expires_at: datetime | None = None
    if "expiry" in fields and fields["expiry"] is not None:
        expires_at = parse_expiry(fields["expiry"])
        if expires_at <= (now or datetime.now(UTC)) and not has_refresh:
            raise GoogleAuthError(
                "access_token is expired; sign in again for a fresh credential"
            )
    return GoogleAuthSummary(
        mode="oauth", kind="subscription", access_expires_at=expires_at
    )


def is_oauth(doc: dict[str, Any]) -> bool:
    """True when `doc` has the OAuth login shape rather than an API key."""
    return "api_key" not in doc and ("token" in doc or "access_token" in doc)


def refresh_token(doc: dict[str, Any]) -> str | None:
    """Return the OAuth `refresh_token` when present and valid, else None."""
    if not is_oauth(doc):
        return None
    try:
        fields = extract_oauth_fields(doc)
    except GoogleAuthError:
        return None
    raw = fields.get("refresh_token")
    return raw if _text(raw) else None


def access_expiry(doc: dict[str, Any]) -> datetime | None:
    """Return the parsed UTC `expiry` timestamp of an OAuth doc, else None."""
    if not is_oauth(doc):
        return None
    try:
        fields = extract_oauth_fields(doc)
        raw = fields.get("expiry")
        return parse_expiry(raw) if raw is not None else None
    except GoogleAuthError:
        return None


def needs_refresh(
    doc: dict[str, Any],
    *,
    now: datetime | None = None,
    margin: timedelta = REFRESH_MARGIN,
) -> bool:
    """True when an OAuth credential has a refresh_token and its access_token is
    expired or within `margin` of expiry."""
    if refresh_token(doc) is None:
        return False
    exp = access_expiry(doc)
    if exp is None:
        return False
    return exp <= (now or datetime.now(UTC)) + margin


def merge_refreshed_token(
    doc: dict[str, Any],
    *,
    access_token: str,
    expiry: datetime,
    expires_in: int | None = None,
    token_type: str | None = None,
    new_refresh_token: str | None = None,
) -> dict[str, Any]:
    """Return a copy of `doc` with fresh token fields, preserving shape and metadata."""
    updated = deepcopy(doc)
    target = updated["token"] if isinstance(updated.get("token"), dict) else updated
    target["access_token"] = access_token
    target["expiry"] = expiry.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if expires_in is not None:
        target["expires_in"] = expires_in
    if token_type is not None:
        target["token_type"] = token_type
    if new_refresh_token is not None:
        target["refresh_token"] = new_refresh_token
    return updated
