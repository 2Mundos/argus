"""Provider quota notices: one pattern set for the API and the NanoClaw channel.

The channel (`integrations/nanoclaw/argus-accounts.ts`) keeps a TypeScript copy of
these patterns; both are tested against `packages/core/tests/fixtures/quota-notices.json`
so they cannot drift (account selection 3.6).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
)

# An expired engine login is handled like an exhausted account: lease another one.
AUTH_EXPIRED_NOTICES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "openai",
        re.compile(
            r"^(?:error:\s*)?(?:http\s*)?401\b.*token_expired|^(?:error:\s*)?token_expired\b"
            r"|^(?:error:\s*)?(?:http\s*)?401\b.*(?:authentication|unauthori[sz]ed)",
            re.I,
        ),
    ),
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
