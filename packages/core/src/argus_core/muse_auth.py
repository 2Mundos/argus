"""Shape of a Muse account credential (muse accounts spec 3). Validation
never echoes a value: errors name fields and rules only, never a key or
token."""

from typing import Any

from pydantic import BaseModel

PROVIDER = "muse"
MAX_TOKEN_LEN = 8192
# The login token comes from an authorised `muse login` session; its exact
# vendor format is unverified, so no prefix is enforced (spec open questions).
_KEYS = ("api_key", "oauth_token")


class MuseAuthError(ValueError):
    """The document is not a usable Muse credential. Names fields only."""


class MuseAuthSummary(BaseModel):
    """What may be shown about a stored credential: no key, no token."""

    mode: str  # "api_key" | "oauth_token"
    kind: str  # Argus AccountKind value ("api_key" | "subscription")


def _token(doc: dict[str, Any], field: str) -> str:
    value = doc.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MuseAuthError(f"{field} must be a non-empty string")
    if len(value) > MAX_TOKEN_LEN:
        raise MuseAuthError(f"{field} is too long")
    if any(c.isspace() or not c.isprintable() for c in value):
        raise MuseAuthError(f"{field} must not contain whitespace")
    return value


def _native_access_token(doc: dict[str, Any]) -> str | None:
    """The access token of a pasted `muse login` auth file
    (`{"providers": {"meta": {"mechanism": "oauth", "access_token": …}}}`,
    the shape the `muse` launcher itself reads), or None."""
    providers = doc.get("providers")
    if not isinstance(providers, dict):
        return None
    meta = providers.get("meta")
    if not isinstance(meta, dict):
        return None
    return (
        meta.get("access_token") if isinstance(meta.get("access_token"), str) else None
    )


def validate(doc: dict[str, Any]) -> MuseAuthSummary:
    """Mutually exclusive shapes, never mixed:

    - `{"api_key": …}`: a Meta API key (`META_API_KEY`, kind `api_key`).
    - `{"oauth_token": …}`: the token of an authorised `muse login`
      session (kind `subscription`).
    - A pasted `muse` auth file (`{"providers": {"meta": …}}`, kind
      `subscription`): extra launcher metadata is tolerated and ignored.
    """
    native = _native_access_token(doc)
    present = [k for k in _KEYS if k in doc]
    if native is not None and not present:
        _token({"access_token": native}, "access_token")
        return MuseAuthSummary(mode="oauth_token", kind="subscription")
    if not present and native is None:
        raise MuseAuthError("needs one of: api_key, oauth_token, or a muse auth file")
    if len(present) > 1 or (native is not None and present):
        raise MuseAuthError(
            "give only one of: api_key, oauth_token, or a muse auth file"
        )
    key = present[0]
    _token(doc, key)
    extra = sorted(set(doc) - {key})
    if extra:
        raise MuseAuthError(f"only the field {key} is allowed; remove {extra[0]}")
    if key == "api_key":
        return MuseAuthSummary(mode="api_key", kind="api_key")
    return MuseAuthSummary(mode="oauth_token", kind="subscription")


def is_oauth(doc: dict[str, Any]) -> bool:
    """True when `doc` has a login shape rather than an API key."""
    return "api_key" not in doc and (
        "oauth_token" in doc or _native_access_token(doc) is not None
    )
