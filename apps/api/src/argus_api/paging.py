"""Shared conventions for list reads (MCP operations 3.1).

Every list returns a page envelope: the items, the total for the filters, and
an opaque keyset cursor bound to those filters. Projections say what they left
out, so a page or a summary is never mistaken for the whole.
"""

import base64
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel

from argus_api.errors import DomainError

View = Literal["summary", "detail"]
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
TEXT_CAP = 4096  # bytes of one free-text field in a detail view


class PageInfo(BaseModel):
    total: int  # every match of the filters, from the same snapshot as `items`
    has_more: bool
    next_cursor: str | None
    omitted: list[str]  # fields left out by the view, or cut in some item


class CursorError(DomainError):
    status = 400
    code = "invalid_cursor"


def clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _fingerprint(filters: dict[str, Any]) -> str:
    text = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def encode_cursor(key: list[Any], filters: dict[str, Any]) -> str:
    raw = json.dumps({"k": key, "f": _fingerprint(filters)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, filters: dict[str, Any]) -> list[Any] | None:
    """The sort key a cursor resumes after; refused if the filters changed."""
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        key, fingerprint = data["k"], data["f"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CursorError("cursor is not one this API returned") from exc
    if fingerprint != _fingerprint(filters):
        raise CursorError(
            "cursor was issued for other filters; repeat them or start over",
            code="cursor_mismatch",
        )
    return key


def cap(value: str | None, field: str, truncated: dict[str, int]) -> str | None:
    """Cut `value` to TEXT_CAP bytes, noting its full length in `truncated`."""
    if value is None:
        return None
    data = value.encode()
    if len(data) <= TEXT_CAP:
        return value
    truncated[field] = len(data)
    return data[:TEXT_CAP].decode(errors="ignore")
