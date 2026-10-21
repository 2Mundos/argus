"""Provider quota notices: one pattern set for the API and the NanoClaw channel.

The channel (`integrations/nanoclaw/argus-accounts.ts`) keeps a TypeScript copy of
these patterns; both are tested against `packages/core/tests/fixtures/quota-notices.json`
so they cannot drift (account selection 3.6).
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal

# Anchored at the start of the notice (after the runner's own prefix): a model that
# merely talks about limits never counts. Keyed by the *account* provider.
QUOTA_NOTICES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Claude Code: "Rate limit [five_hour] (resets …)", "Out of credits",
    # "You've hit your session limit · resets 2am (UTC)", "Usage limit reached".
    (
        "anthropic",
        re.compile(
            r"^(rate limit|out of credits|usage limit reached"
            r"|you(?:'|\u2019)ve hit your (?:\w+ )?limit|you have hit your (?:\w+ )?limit)\b",
            re.I | re.A,
        ),
    ),
    (
        "openai",
        re.compile(
            r"^(usage_limit_(?:reached|exceeded)|usagelimitexceeded|insufficient_quota"
            r"|you(?:'|\u2019)ve (?:hit|reached) your usage limit"
            r"|you have (?:hit|reached) your usage limit"
            r"|you exceeded your current quota)",
            re.I,
        ),
    ),
    # Custom OpenAI-compatible providers (OpenRouter): HTTP 402 (credits) and 429
    # (rate limit), as `quota_notice_from_response` words them.
    (
        "custom",
        re.compile(
            r"^(?:http\s*)?(?:402|429)\b|^(?:insufficient credits|payment required"
            r"|rate limit)",
            re.I,
        ),
    ),
)

# An expired engine login is handled like an exhausted account: lease another one.
_AUTH_EXPIRED = re.compile(
    r"^(?:error:\s*)?(?:http\s*)?401\b.*token_expired|^(?:error:\s*)?token_expired\b"
    r"|^(?:error:\s*)?(?:http\s*)?401\b.*(?:authentication|unauthori[sz]ed)",
    re.I,
)
AUTH_EXPIRED_NOTICES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai", _AUTH_EXPIRED),
    ("custom", _AUTH_EXPIRED),
)

# Structured `codexErrorInfo` / `error.code` values of the Codex app-server that mean
# "this account's quota is spent", keyed by the same account provider.
STRUCTURED_QUOTA_CODES = frozenset(
    {
        "usagelimitexceeded",
        "usage_limit_exceeded",
        "usage_limit_reached",
        "insufficient_quota",
    }
)
# Failures that are the network's, not the account's or the task's.
NETWORK_NOTICES = re.compile(
    r"\b(?:etimedout|econnreset|econnrefused|enotfound|eai_again|epipe|ehostunreach)\b"
    r"|fetch failed|network (?:error|is unreachable|unreachable)"
    r"|connection (?:reset|refused|closed|timed out)"
    r"|stream disconnected|timed? ?out|httpconnectionfailed|temporarily unavailable"
    r"|service unavailable|bad gateway|gateway time-?out",
    re.I,
)

_PREFIX = re.compile(
    r"^(?:(?:codex |claude )?error:\s*|turn failed:\s*|reconnecting\.{3}\s*\d+/\d+:\s*)",
    re.I,
)
_RESET_MARKER = re.compile(
    r"\(resets |\bresets? (?:at |on )?|try again (?:at|in) ", re.I
)
_RESETS = re.compile(r"\(resets ([^)]+)\)", re.I)
_RESETS_CLOCK = re.compile(
    r"\bresets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*\(UTC\)", re.I | re.A
)
REASON_MAX = 500

ResetStatus = Literal["stated", "ambiguous", "absent"]
FailureClass = Literal[
    "quota_exhausted",
    "auth_expired",
    "network_unavailable",
    "runtime_switch_failed",
    "implementation",
]


@dataclass(frozen=True)
class QuotaHit:
    reason: str  # the sanitized notice, as the provider wrote it
    until: datetime | None  # the provider's reset time, only when it states a zone
    code: Literal["quota_exhausted", "auth_expired"] = "quota_exhausted"
    # `stated`: `until` is the provider's; `ambiguous`: the notice names a time we
    # cannot place (no zone, unknown zone): `reset_text` keeps its words and `until`
    # is None; `absent`: no reset in the notice.
    reset_status: ResetStatus = "absent"
    reset_text: str | None = None


def _fold(notice: str) -> str:
    """Control characters folded to spaces, whitespace collapsed: the text that is
    classified (URLs intact, so a structured error document still parses)."""
    return re.sub(r"\s{2,}", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", notice)).strip()


def sanitize_notice(notice: str, limit: int = REASON_MAX) -> str:
    """One line of provider text: control characters folded to spaces, bearer-like
    tokens and URL query strings dropped, bounded. Never a prompt or a credential."""
    text = re.sub(
        r"(?i)\b(?:bearer\s+|sk-|eyJ)[A-Za-z0-9._~+/=-]{8,}",
        "[redacted]",
        _fold(notice),
    )
    text = re.sub(r"(https?://[^\s?#)]+)[?#][^\s)]*", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()[:limit]


def _unwrap(body: str) -> str:
    """A structured Codex error (`{"error": {"message", "codexErrorInfo"}}`, or the
    same fields at top level) as the text its code or message would have been."""
    if not body.startswith("{"):
        return body
    try:
        doc = json.loads(body)
    except ValueError:
        return body
    err = doc.get("error", doc) if isinstance(doc, dict) else None
    if isinstance(err, str):
        return err
    if not isinstance(err, dict):
        return body
    message = err.get("message")
    for key in ("codexErrorInfo", "code", "type"):
        value = err.get(key)
        if isinstance(value, str) and value.lower() in STRUCTURED_QUOTA_CODES:
            return f"{value}: {message}" if isinstance(message, str) else value
    return message if isinstance(message, str) else body


def classify_quota(
    notice: str, provider: str | None = None, now: datetime | None = None
) -> QuotaHit | None:
    """The quota (or expired-login) hit a failure notice reports, or None. With
    `provider`, only that account provider's messages count. `now` anchors a
    clock-time reset ("resets 2am (UTC)"); it defaults to the current time."""
    text = sanitize_notice(notice)
    folded = _fold(notice)
    body = _PREFIX.sub("", _unwrap(_PREFIX.sub("", folded, count=1)), count=1)
    for patterns, code in (
        (AUTH_EXPIRED_NOTICES, "auth_expired"),
        (QUOTA_NOTICES, "quota_exhausted"),
    ):
        if any(p.search(body) and provider in (None, name) for name, p in patterns):
            if code == "auth_expired":
                return QuotaHit(text, None, "auth_expired")
            until, status, words = reset_of(body, now)
            return QuotaHit(text, until, "quota_exhausted", status, words)
    return None


def failure_class(
    notice: str, reason: str = "", provider: str | None = None
) -> FailureClass:
    """What kind of failure a task error is, for REST/MCP/UI: a provider quota, an
    expired login, the network, a failed runtime switch, or (the default) the work."""
    if reason == "runtime_switch_failed":
        return "runtime_switch_failed"
    hit = classify_quota(notice, provider)
    if hit is not None:
        return hit.code
    if NETWORK_NOTICES.search(notice):
        return "network_unavailable"
    return "implementation"


def reset_of(
    body: str, now: datetime | None = None
) -> tuple[datetime | None, ResetStatus, str | None]:
    """The reset a quota notice states: its time only when the notice carries a zone
    (ISO with offset or `Z`, or a `(UTC)` clock time); otherwise the provider's words
    are kept as text and the time stays unknown (never a guessed zone)."""
    until = _reset(body, now)
    marker = _RESET_MARKER.search(body)
    words: str | None = None
    if marker is not None:
        rest = body[marker.end() :].split("\n", 1)[0]
        if marker.group(0).startswith("("):
            rest = rest.split(")", 1)[0]
        rest = re.split(r"\.(?:\s|$)", rest, maxsplit=1)[0]
        words = sanitize_notice(rest, 80).rstrip(".") or None
    if until is not None:
        return until, "stated", words
    return None, ("ambiguous" if words else "absent"), words


def _reset(body: str, now: datetime | None) -> datetime | None:
    """`(resets <ISO>)`, or a UTC clock time: the next such time strictly after `now`."""
    m = _RESETS.search(body)
    if m is not None:
        try:
            when = datetime.fromisoformat(m.group(1).strip())
        except ValueError:
            pass
        else:
            if when.tzinfo is not None:
                return when
    clock = _RESETS_CLOCK.search(body)
    if clock is None:
        return None
    hour12, minute = int(clock.group(1)), int(clock.group(2) or 0)
    if not 1 <= hour12 <= 12 or minute > 59:
        return None
    hour = hour12 % 12 + (12 if clock.group(3).lower() == "p" else 0)
    base = (now or datetime.now(UTC)).astimezone(UTC)
    at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > base else at + timedelta(days=1)


def quota_notice_from_response(
    status: int, headers: Mapping[str, str], now: datetime | None = None
) -> str | None:
    """The quota notice for an HTTP 402/429 from a custom provider, or None for any
    other status. The reset comes from `Retry-After` (seconds or an HTTP date), else
    `X-RateLimit-Reset` (epoch seconds or milliseconds). The body is never used:
    only the status and a reset time reach the notice (and so a log or an event)."""
    if status not in (402, 429):
        return None
    base = (now or datetime.now(UTC)).astimezone(UTC)
    lowered = {k.lower(): v for k, v in headers.items()}
    until = _retry_after(lowered.get("retry-after"), base) or _epoch(
        lowered.get("x-ratelimit-reset"), base
    )
    what = "insufficient credits" if status == 402 else "rate limited"
    suffix = f" (resets {until.isoformat()})" if until else ""
    return f"HTTP {status} {what}{suffix}"


def _retry_after(value: str | None, base: datetime) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return base + timedelta(seconds=min(int(value), 7 * 86400))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    return when if when > base else None


def _epoch(value: str | None, base: datetime) -> datetime | None:
    if not value:
        return None
    try:
        raw = float(value)
    except ValueError:
        return None
    seconds = raw / 1000 if raw > 1e11 else raw  # OpenRouter sends milliseconds
    try:
        when = datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return when if base < when <= base + timedelta(days=7) else None
