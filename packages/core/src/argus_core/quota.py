"""Provider quota notices: one pattern set for the API and the NanoClaw channel.

The channel (`integrations/nanoclaw/argus-accounts.ts`) keeps a TypeScript copy of
these patterns; both are tested against `packages/core/tests/fixtures/quota-notices.json`
so they cannot drift (account selection 3.6).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# Anchored at the start of the notice (after the runner's own prefix): a model that
# merely talks about limits never counts. Keyed by the *account* provider.
QUOTA_NOTICES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"^(rate limit|out of credits)\b", re.I)),
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
REASON_MAX = 500


@dataclass(frozen=True)
class QuotaHit:
    reason: str
    until: datetime | None  # the provider's reset time, if the notice states one


def classify_quota(notice: str, provider: str | None = None) -> QuotaHit | None:
    """The quota (or expired-login) hit a failure notice reports, or None. With
    `provider`, only that account provider's messages count."""
    text = notice.strip()
    body = _PREFIX.sub("", text, count=1)
    for patterns, with_reset in ((AUTH_EXPIRED_NOTICES, False), (QUOTA_NOTICES, True)):
        if any(p.search(body) and provider in (None, name) for name, p in patterns):
            until = _reset(body) if with_reset else None
            return QuotaHit(text[:REASON_MAX], until)
    return None


def _reset(body: str) -> datetime | None:
    m = _RESETS.search(body)
    if m is None:
        return None
    try:
        when = datetime.fromisoformat(m.group(1).strip())
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)
