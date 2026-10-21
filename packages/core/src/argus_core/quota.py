"""Provider quota notices: one pattern set for the API and the NanoClaw channel.

The channel (`integrations/nanoclaw/argus-accounts.ts`) keeps a TypeScript copy of
these patterns; both are tested against `packages/core/tests/fixtures/quota-notices.json`
so they cannot drift (account selection 3.6).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

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
            r"^(usage_limit_reached|insufficient_quota|you(?:'|\u2019)ve hit your usage"
            r" limit|you have hit your usage limit)",
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

_PREFIX = re.compile(r"^(?:error:\s*|reconnecting\.{3}\s*\d+/\d+:\s*)", re.I)
_RESETS = re.compile(r"\(resets ([^)]+)\)", re.I)
_RESETS_CLOCK = re.compile(
    r"\bresets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*\(UTC\)", re.I | re.A
)
REASON_MAX = 500


@dataclass(frozen=True)
class QuotaHit:
    reason: str
    until: datetime | None  # the provider's reset time, if the notice states one


def classify_quota(
    notice: str, provider: str | None = None, now: datetime | None = None
) -> QuotaHit | None:
    """The quota (or expired-login) hit a failure notice reports, or None. With
    `provider`, only that account provider's messages count. `now` anchors a
    clock-time reset ("resets 2am (UTC)"); it defaults to the current time."""
    text = notice.strip()
    body = _PREFIX.sub("", text, count=1)
    for patterns, with_reset in ((AUTH_EXPIRED_NOTICES, False), (QUOTA_NOTICES, True)):
        if any(p.search(body) and provider in (None, name) for name, p in patterns):
            until = _reset(body, now) if with_reset else None
            return QuotaHit(text[:REASON_MAX], until)
    return None


def _reset(body: str, now: datetime | None) -> datetime | None:
    """`(resets <ISO>)`, or a UTC clock time: the next such time strictly after `now`."""
    m = _RESETS.search(body)
    if m is not None:
        try:
            when = datetime.fromisoformat(m.group(1).strip())
        except ValueError:
            pass
        else:
            return when if when.tzinfo else when.replace(tzinfo=UTC)
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
