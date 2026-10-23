"""Read-only quota requests to a provider's own account endpoints (account usage
3.3): Claude's OAuth usage and OpenRouter's key. Each is a single GET with the
account's own credential; redirects are never followed, nothing from a response
body reaches an error, and a credential never reaches a log."""

import asyncio
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2 as httpx
from argus_core.accounts import QuotaReport, QuotaStatusReason

from argus_api.accounts.usage import CreditReading

log = logging.getLogger(__name__)

TIMEOUT_S = 15.0
MAX_BODY = 1024 * 1024
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"
FIVE_HOURS, WEEK = 300, 10080
# (response key, report) of the flat shape: `utilization` is the percent USED.
_FLAT_WINDOWS: dict[str, dict[str, Any]] = {
    "five_hour": {"window_minutes": FIVE_HOURS},
    "seven_day": {"window_minutes": WEEK},
    "seven_day_opus": {"pool": "weekly_opus"},
    "seven_day_sonnet": {"pool": "weekly_sonnet"},
}


class ReadError(Exception):
    """A read that did not yield a figure. `code` is a `QuotaStatusReason`; the
    message is ours, never the provider's text."""

    def __init__(self, code: QuotaStatusReason, message: str) -> None:
        super().__init__(message)
        self.code = code


def _number(value: Any) -> float | None:
    """A finite number, or a numeric string (int64 and decimals may arrive quoted);
    anything else is absent."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _percent(value: Any) -> float | None:
    n = _number(value)
    return None if n is None else min(max(n, 0.0), 100.0)


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _pool_name(text: str) -> str:
    return (
        "weekly_"
        + (re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "scoped")[:30]
    )


@dataclass
class ClaudeUsage:
    windows: list[QuotaReport] = field(default_factory=list)
    locked: list[str] = field(
        default_factory=list
    )  # names only, never the provider's text


def claude_usage(payload: Any) -> ClaudeUsage:
    """The windows of `GET /api/oauth/usage`. The flat keys (`five_hour`,
    `seven_day`, ... each `{utilization, resets_at, locked_reason, ...}` or null) and
    the structured `limits` list (`kind`, `percent`, `resets_at`, `is_active`, ...)
    both give the share USED, never the share left, as a number or a numeric string.
    A null window is absent, not zero. A window with a `locked_reason` is locked, not
    unused: it is listed in `locked` and has no figure. Dollar fields, `severity`,
    `extra_usage` and `spend` are not read."""
    out = ClaudeUsage()
    if not isinstance(payload, dict):
        return out
    seen: set[str] = set()
    for key, target in _FLAT_WINDOWS.items():
        w = payload.get(key)
        if not isinstance(w, dict):
            continue
        name = QuotaReport(used_percent=0, **target).window
        if w.get("locked_reason"):
            out.locked.append(name)
            seen.add(name)
            continue
        used = _percent(w.get("utilization"))
        if used is not None:
            out.windows.append(
                QuotaReport(
                    used_percent=used, resets_at=_when(w.get("resets_at")), **target
                )
            )
            seen.add(name)
    limits = payload.get("limits")
    for item in limits if isinstance(limits, list) else []:
        if not isinstance(item, dict) or item.get("is_active") is False:
            continue
        kind, resets = item.get("kind"), _when(item.get("resets_at"))
        target: dict[str, Any]
        if kind == "session":  # its duration is not in the response
            target = {"pool": "session"}
            twin = "300m"
        elif kind == "weekly_all":
            target, twin = {"window_minutes": WEEK}, "10080m"
        elif kind == "weekly_scoped":
            scope = item.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            target = {"pool": _pool_name(name if isinstance(name, str) else "")}
            twin = target["pool"]
        else:
            continue
        window = QuotaReport.model_validate({**target, "used_percent": 0}).window
        if window in seen or twin in seen:  # the flat key already said it
            continue
        used = _percent(item.get("percent"))
        if used is not None:
            out.windows.append(
                QuotaReport.model_validate(
                    {**target, "used_percent": used, "resets_at": resets}
                )
            )
            seen.add(window)
    return out


def claude_windows(payload: Any) -> list[QuotaReport]:
    return claude_usage(payload).windows


def _failure(status: int, headers: Mapping[str, str], *, oauth: bool) -> ReadError:
    """A non-200 answer as a diagnosis. 403 on the OAuth usage call is the token
    lacking the profile scope (a `setup-token` credential is inference-only)."""
    if status == 401:
        return ReadError("credential_rejected", "the provider rejected the credential")
    if status == 403 and oauth:
        return ReadError(
            "oauth_scope_missing",
            "the token lacks the scope the usage read needs (sign in with a full "
            "Claude login, not `setup-token`)",
        )
    if status == 403:
        return ReadError("credential_rejected", "the provider refused this credential")
    if status == 429:
        after = headers.get("retry-after", "")
        wait = f", retry after {after}s" if after.isdigit() else ""
        return ReadError(
            "provider_rate_limited", f"the provider rate-limited the read{wait}"
        )
    return ReadError("fetch_failed", f"the provider answered HTTP {status}")


async def _get(
    url: str,
    headers: dict[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_S, transport=transport, follow_redirects=False
        ) as client:
            return await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("quota read failed: %s", type(exc).__name__)
        raise ReadError("fetch_failed", f"unreachable ({type(exc).__name__})") from None


def _json(res: httpx.Response) -> Any:
    if len(res.content) > MAX_BODY:
        raise ReadError("bad_response", "response too large")
    try:
        return res.json()
    except ValueError:
        raise ReadError("bad_response", "response is not JSON") from None


async def read_claude(
    access_token: str, transport: httpx.AsyncBaseTransport | None = None
) -> ClaudeUsage:
    res = await _get(
        CLAUDE_USAGE_URL,
        {
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": CLAUDE_BETA,
            "Accept": "application/json",
        },
        transport,
    )
    if res.status_code != 200:
        raise _failure(res.status_code, res.headers, oauth=True)
    usage = claude_usage(_json(res))
    if not usage.windows:
        if usage.locked:
            raise ReadError(
                "window_locked",
                f"the provider locked: {', '.join(usage.locked)} (no usage figure)",
            )
        raise ReadError("no_windows", "the usage response carried no window")
    return usage


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def openrouter_credit(payload: Any) -> CreditReading:
    """`GET /key`: `data.limit` (null: no cap), `limit_remaining`, `usage` (credits
    used, all time), `limit_reset`, `is_free_tier`. A capped key must state its
    remaining credit, or the reading is refused rather than guessed."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ReadError("bad_response", "response has no key data")
    limit, remaining = _amount(data.get("limit")), _amount(data.get("limit_remaining"))
    if data.get("limit") is not None and (limit is None or remaining is None):
        raise ReadError("bad_response", "a capped key reported no remaining credit")
    reset = data.get("limit_reset")
    free = data.get("is_free_tier")
    return CreditReading(
        limit=limit,
        remaining=remaining,
        used=_amount(data.get("usage")),
        limit_reset=reset if isinstance(reset, str) and len(reset) <= 20 else None,
        free_tier=free if isinstance(free, bool) else None,
    )


async def read_openrouter(
    base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None
) -> CreditReading:
    res = await _get(
        f"{base_url.rstrip('/')}/key",
        {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        transport,
    )
    if res.status_code != 200:
        raise _failure(res.status_code, res.headers, oauth=False)
    return openrouter_credit(_json(res))


# -- Cursor ---------------------------------------------------------------------------

CURSOR_HOST = "https://api2.cursor.sh"
CURSOR_DASHBOARD = "/aiserver.v1.DashboardService/"
CURSOR_CALLS = (
    "GetCreditGrantsBalance",
    "GetUsageLimitStatusAndActiveGrants",
    "GetPlanInfo",
)
_SAFE_TEXT = re.compile(r"[^A-Za-z0-9 _:./-]")


@dataclass
class CursorReading:
    credit: CreditReading | None = None
    note: str = ""  # plan period and cycle only, when the provider stated them


def _cents(value: Any) -> int | None:
    """An int64 cents field: a number or a quoted integer. Absent stays absent; a
    missing field is never 0."""
    n = _number(value)
    return int(n) if n is not None and n >= 0 and n == int(n) else None


def _usd(cents: int | None) -> float | None:
    return None if cents is None else cents / 100


def _epoch_ms(value: Any) -> datetime | None:
    n = _number(value)
    if n is None or n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _plan_text(value: Any) -> str:
    return _SAFE_TEXT.sub("", value)[:40].strip() if isinstance(value, str) else ""


def cursor_reading(balance: Any, status: Any, plan: Any) -> CursorReading:
    """Cursor's dashboard answers (Connect JSON; int64 arrive as strings). Credit is
    read only from explicit numeric fields:
    - `GetCreditGrantsBalance`: `totalCents`, `usedCents`, `creditBalanceCents`;
    - `GetUsageLimitStatusAndActiveGrants`: `activeGrants[]` `remainingCents` /
      `totalCents` (summed, only when every grant states the field), and the cycle
      reset `resetAtMs`;
    - `GetPlanInfo`: `planInfo.billingCycleEnd` as a reset when it is a date, and
      `includedUsagePeriod` for the note.
    `{}` or a missing field is absent: no credit, never 0."""
    bal = balance if isinstance(balance, dict) else {}
    st = status if isinstance(status, dict) else {}
    info = plan.get("planInfo") if isinstance(plan, dict) else None
    info = info if isinstance(info, dict) else {}
    total, used, left = (
        _cents(bal.get(k)) for k in ("totalCents", "usedCents", "creditBalanceCents")
    )
    grants = [g for g in st.get("activeGrants") or [] if isinstance(g, dict)]
    resets = _epoch_ms(st.get("resetAtMs"))
    cycle = info.get("billingCycleEnd")
    if resets is None and isinstance(cycle, str):
        resets = _epoch_ms(cycle) if cycle.strip().isdigit() else _when(cycle)
    if total is None and left is None and grants:
        gt = [x for g in grants if (x := _cents(g.get("totalCents"))) is not None]
        gr = [x for g in grants if (x := _cents(g.get("remainingCents"))) is not None]
        if len(gt) == len(gr) == len(grants):  # every grant states both
            total, left = sum(gt), sum(gr)
            used = total - left
    if left is None and total is not None and used is not None:
        left = max(total - used, 0)
    credit = None
    if left is not None or (total is not None and used is not None):
        credit = CreditReading(
            limit=_usd(total), remaining=_usd(left), used=_usd(used), resets_at=resets
        )
    parts = [
        f"plan period {p}" for p in [_plan_text(info.get("includedUsagePeriod"))] if p
    ] + ([f"cycle ends {resets.date().isoformat()}"] if resets else [])
    return CursorReading(credit=credit, note=", ".join(parts))


async def read_cursor(
    access_token: str, transport: httpx.AsyncBaseTransport | None = None
) -> CursorReading:
    """The account's own OAuth login against `api2.cursor.sh` dashboard calls: three
    POSTs of `{}` as Connect JSON, one client, 15 s in all, no redirects."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
    }

    async def call_all() -> list[Any]:
        bodies = []
        async with httpx.AsyncClient(
            base_url=CURSOR_HOST,
            timeout=TIMEOUT_S,
            transport=transport,
            follow_redirects=False,
        ) as client:
            for name in CURSOR_CALLS:
                res = await client.post(
                    CURSOR_DASHBOARD + name, headers=headers, content=b"{}"
                )
                if res.status_code != 200:
                    raise _failure(res.status_code, res.headers, oauth=False)
                bodies.append(_json(res))
        return bodies

    try:
        balance, status, plan = await asyncio.wait_for(call_all(), TIMEOUT_S)
    except (httpx.HTTPError, TimeoutError) as exc:
        log.warning("cursor quota read failed: %s", type(exc).__name__)
        raise ReadError("fetch_failed", f"unreachable ({type(exc).__name__})") from None
    if not all(isinstance(b, dict) for b in (balance, status, plan)):
        raise ReadError("bad_response", "response is not a JSON object")
    return cursor_reading(balance, status, plan)
