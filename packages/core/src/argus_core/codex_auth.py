"""Shape of a Codex `auth.json` (codex accounts spec 3). Validation never echoes
a value: errors name fields only."""

import base64
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

PROVIDER = "openai"
_TOKENS = ("id_token", "access_token", "refresh_token")


class CodexAuthError(ValueError):
    """The document is not a usable Codex login. The message names fields only."""


class CodexAuthSummary(BaseModel):
    """What may be shown about a stored login: no token, no id, no email."""

    mode: str  # "chatgpt" | "apikey"
    kind: str  # Argus AccountKind value
    access_expires_at: datetime | None = None


def _jwt_expiry(token: str) -> datetime | None:
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        if not isinstance(claims, dict):  # array, string, null: expiry unknown
            return None
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int | float):
            return None
        return datetime.fromtimestamp(exp, UTC)
    except (
        IndexError,
        ValueError,
        TypeError,
        OSError,
        OverflowError,
        AttributeError,
        RecursionError,  # a deeply nested payload
    ):
        return None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate(doc: dict[str, Any]) -> CodexAuthSummary:
    """Accept a ChatGPT login (`auth_mode: chatgpt`, a token set) or an API-key
    document (`auth_mode: apikey`). Anything else is rejected."""
    mode = doc.get("auth_mode")
    if mode == "apikey":
        if not _text(doc.get("OPENAI_API_KEY")):
            raise CodexAuthError("apikey auth.json needs OPENAI_API_KEY")
        return CodexAuthSummary(mode="apikey", kind="api_key")
    if mode != "chatgpt":
        raise CodexAuthError("auth_mode must be 'chatgpt' or 'apikey'")
    tokens = doc.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexAuthError("chatgpt auth.json needs a tokens object")
    missing = [name for name in _TOKENS if not _text(tokens.get(name))]
    if missing:
        raise CodexAuthError(f"tokens is missing: {', '.join(missing)}")
    return CodexAuthSummary(
        mode="chatgpt",
        kind="subscription",
        access_expires_at=_jwt_expiry(tokens["access_token"]),
    )
