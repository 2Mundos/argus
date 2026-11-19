"""Shape of a Google (Antigravity) account credential (google accounts spec 3).
Validation never echoes a value: errors name fields and rules only, never a key
or token."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

PROVIDER = "google"
MAX_TOKEN_LEN = 8192
_API_KEY_ONLY = frozenset({"api_key"})
_OAUTH_TOP_KEYS = frozenset({"token", "auth_method"})
_OAUTH_FLAT_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "expiry",
        "expires_in",
        "token_type",
        "auth_method",
    }
)
_OAUTH_INNER_KEYS = frozenset(
    {"access_token", "refresh_token", "expiry", "expires_in", "token_type"}
)


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
    """Return the token dict from either native `{"token": {...}}` or flat form."""
    if "token" in doc:
        inner = doc.get("token")
        if not isinstance(inner, dict):
            raise GoogleAuthError("token must be a JSON object")
        extra_top = sorted(set(doc) - _OAUTH_TOP_KEYS)
        if extra_top:
            raise GoogleAuthError(
                f"unexpected field in OAuth credential: {extra_top[0]}"
            )
        extra_inner = sorted(set(inner) - _OAUTH_INNER_KEYS)
        if extra_inner:
            raise GoogleAuthError(f"unexpected field in token object: {extra_inner[0]}")
        return inner
    extra = sorted(set(doc) - _OAUTH_FLAT_KEYS)
    if extra:
        raise GoogleAuthError(f"unexpected field in OAuth credential: {extra[0]}")
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
    if "refresh_token" in fields and fields["refresh_token"] is not None:
        _token(fields, "refresh_token")
    expires_at: datetime | None = None
    if "expiry" in fields and fields["expiry"] is not None:
        expires_at = parse_expiry(fields["expiry"])
        if expires_at <= (now or datetime.now(UTC)):
            raise GoogleAuthError(
                "access_token is expired; sign in again for a fresh credential"
            )
    return GoogleAuthSummary(
        mode="oauth", kind="subscription", access_expires_at=expires_at
    )
