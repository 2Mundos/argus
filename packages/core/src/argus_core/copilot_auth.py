"""Shape of a Copilot account credential (copilot accounts spec 3). Validation
never echoes a value: errors name the field and the rule, not the token or any
part of it."""

from typing import Any

from pydantic import BaseModel

PROVIDER = "copilot"
MAX_TOKEN_LEN = 8192
# Fine-grained, OAuth and user-to-server tokens. `ghp_` (classic) is ignored by
# the CLI, so it is refused by name rather than stored and failing at first use.
_PREFIXES = ("github_pat_", "gho_", "ghu_")
_CLASSIC_PREFIX = "ghp_"


class CopilotAuthError(ValueError):
    """The document is not a usable Copilot credential. Names fields only."""


class CopilotAuthSummary(BaseModel):
    """What may be shown about a stored credential: no token."""

    mode: str  # "token"
    kind: str  # Argus AccountKind value


def validate(doc: dict[str, Any]) -> CopilotAuthSummary:
    """A single `{"token": …}`: a fine-grained personal access token, or an OAuth
    token, of a Copilot seat. Whether the token carries the Copilot permission is
    only known at first use."""
    extra = sorted(set(doc) - {"token"})
    if extra:
        raise CopilotAuthError(f"only the field token is allowed; remove {extra[0]}")
    value = doc.get("token")
    if not isinstance(value, str) or not value.strip():
        raise CopilotAuthError("token must be a non-empty string")
    if len(value) > MAX_TOKEN_LEN:
        raise CopilotAuthError("token is too long")
    if any(c.isspace() or not c.isprintable() for c in value):
        raise CopilotAuthError("token must not contain whitespace")
    if value.startswith(_CLASSIC_PREFIX):
        raise CopilotAuthError(
            "token is a classic personal access token, which the Copilot CLI "
            "ignores; use a fine-grained token or an OAuth token"
        )
    if not value.startswith(_PREFIXES):
        raise CopilotAuthError(f"token must start with one of: {', '.join(_PREFIXES)}")
    return CopilotAuthSummary(mode="token", kind="subscription")
