"""Shape of an Anthropic account credential (claude accounts spec 3). Validation
never echoes a value: errors name fields only."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

PROVIDER = "anthropic"
MAX_TOKEN_LEN = 4096
_OAUTH_PREFIX = "sk-ant-oat"
_API_PREFIX = "sk-ant-api"
_KEYS = ("api_key", "oauth_token", "claudeAiOauth")


class ClaudeAuthError(ValueError):
    """The document is not a usable Anthropic credential. Names fields only."""


class ClaudeAuthSummary(BaseModel):
    """What may be shown about a stored credential: no token, no id, no email."""

    mode: str  # "api_key" | "oauth_token" | "claude_ai_oauth"
    kind: str  # Argus AccountKind value
    access_expires_at: datetime | None = None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _token(doc: dict[str, Any], field: str, label: str) -> str:
    value = doc.get(field)
    if not _text(value):
        raise ClaudeAuthError(f"{label} must be a non-empty string")
    assert isinstance(value, str)
    if len(value) > MAX_TOKEN_LEN:
        raise ClaudeAuthError(f"{label} is too long")
    if any(c.isspace() or not c.isprintable() for c in value):
        raise ClaudeAuthError(f"{label} must not contain whitespace")
    return value


def _expiry(value: Any) -> datetime | None:
    """`expiresAt` as epoch milliseconds or an ISO date; None when absent."""
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise TypeError
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value / 1000, UTC)
        if isinstance(value, str) and value.strip():
            when = datetime.fromisoformat(value.strip())
            return when if when.tzinfo else when.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    raise ClaudeAuthError("claudeAiOauth.expiresAt is not a date")


def validate(doc: dict[str, Any], *, now: datetime | None = None) -> ClaudeAuthSummary:
    present = [k for k in _KEYS if k in doc]
    if not present:
        raise ClaudeAuthError("needs one of: api_key, oauth_token, claudeAiOauth")
    if len(present) > 1:
        raise ClaudeAuthError(f"give only one of: {', '.join(present)}")
    key = present[0]
    if key == "api_key":
        if _token(doc, "api_key", "api_key").startswith(_OAUTH_PREFIX):
            raise ClaudeAuthError("api_key holds an OAuth token; use oauth_token")
        return ClaudeAuthSummary(mode="api_key", kind="api_key")
    if key == "oauth_token":
        if _token(doc, "oauth_token", "oauth_token").startswith(_API_PREFIX):
            raise ClaudeAuthError("oauth_token holds an API key; use api_key")
        return ClaudeAuthSummary(mode="oauth_token", kind="subscription")
    oauth = doc["claudeAiOauth"]
    if not isinstance(oauth, dict):
        raise ClaudeAuthError("claudeAiOauth must be an object")
    missing = [f for f in ("accessToken", "refreshToken") if not _text(oauth.get(f))]
    if missing:
        raise ClaudeAuthError(f"claudeAiOauth is missing: {', '.join(missing)}")
    for field in ("accessToken", "refreshToken"):
        _token(oauth, field, f"claudeAiOauth.{field}")
    expires = _expiry(oauth.get("expiresAt"))
    if expires is not None and expires <= (now or datetime.now(UTC)):
        raise ClaudeAuthError(
            "claudeAiOauth.expiresAt is in the past; sign in again for a fresh credential"
        )
    return ClaudeAuthSummary(
        mode="claude_ai_oauth", kind="subscription", access_expires_at=expires
    )
