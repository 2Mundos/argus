"""Shape of a Cursor account credential (cursor accounts spec 3). Validation never
echoes a value: errors name fields only."""

import base64
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

PROVIDER = "cursor"
MAX_TOKEN_LEN = 8192
_KEYS = ("api_key", "accessToken")


class CursorAuthError(ValueError):
    """The document is not a usable Cursor credential. Names fields only."""


class CursorAuthSummary(BaseModel):
    """What may be shown about a stored credential: no token, no id, no email."""

    mode: str  # "api_key" | "oauth"
    kind: str  # Argus AccountKind value
    access_expires_at: datetime | None = None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _token(doc: dict[str, Any], field: str) -> str:
    value = doc.get(field)
    if not _text(value):
        raise CursorAuthError(f"{field} must be a non-empty string")
    assert isinstance(value, str)
    if len(value) > MAX_TOKEN_LEN:
        raise CursorAuthError(f"{field} is too long")
    if any(c.isspace() or not c.isprintable() for c in value):
        raise CursorAuthError(f"{field} must not contain whitespace")
    return value


def jwt_expiry(token: str) -> datetime | None:
    """`exp` of a JWT-shaped token, read without checking its signature (the
    issuer's key is not ours); None when the token is not a JWT with an `exp`."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims["exp"]
        if isinstance(exp, bool) or not isinstance(exp, int | float):
            return None
        return datetime.fromtimestamp(exp, UTC)
    except (ValueError, KeyError, TypeError, OverflowError, OSError):
        return None


def validate(doc: dict[str, Any], *, now: datetime | None = None) -> CursorAuthSummary:
    """Two shapes, never mixed:

    - `{"api_key": …}`: a Cursor API key (kind `api_key`). The channel exchanges it
      for a short-lived access token on the trusted host.
    - `{"accessToken": …, "refreshToken": …}`: an imported `agent login` (kind
      `subscription`). Only `accessToken` is ever used, so it must be a JWT whose
      `exp` is in the future; `refreshToken` is stored but never leaves Argus.

    A Cursor CLI settings file (`cli-config.json`: permissions, editor, model,
    authInfo) holds neither and is rejected."""
    present = [k for k in _KEYS if k in doc]
    if not present:
        raise CursorAuthError(
            "needs one of: api_key, or accessToken with refreshToken "
            "(a Cursor settings file is not a credential)"
        )
    if len(present) > 1:
        raise CursorAuthError(f"give only one of: {', '.join(present)}")
    if present[0] == "api_key":
        value = _token(doc, "api_key")
        if jwt_expiry(value) is not None:
            raise CursorAuthError(
                "api_key holds a login token (a JWT); put it in accessToken "
                "together with refreshToken"
            )
        return CursorAuthSummary(mode="api_key", kind="api_key")
    missing = [f for f in ("accessToken", "refreshToken") if not _text(doc.get(f))]
    if missing:
        raise CursorAuthError(f"login credential is missing: {', '.join(missing)}")
    access = _token(doc, "accessToken")
    _token(doc, "refreshToken")
    expires = jwt_expiry(access)
    if expires is None:
        raise CursorAuthError(
            "accessToken is not a JWT with an exp claim; export the login again"
        )
    if expires <= (now or datetime.now(UTC)):
        raise CursorAuthError(
            "accessToken is expired; sign in again for a fresh credential"
        )
    return CursorAuthSummary(
        mode="oauth", kind="subscription", access_expires_at=expires
    )
