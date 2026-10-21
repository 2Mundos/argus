import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argus_core.quota import classify_quota, failure_class

FIXTURE = Path(__file__).parent / "fixtures" / "quota-notices.json"
CASES = json.loads(FIXTURE.read_text())


@pytest.mark.parametrize(
    "case", CASES, ids=lambda c: f"{c['provider']}:{c['notice'][:30]}"
)
def test_shared_fixture(case: dict) -> None:
    now = datetime.fromisoformat(case["now"]) if "now" in case else None
    hit = classify_quota(case["notice"], case["provider"], now)
    assert (hit is not None) is case["exhausted"]
    if hit is not None and case["until"]:
        assert hit.until == datetime.fromisoformat(case["until"])
    elif hit is not None:
        assert hit.until is None
    if hit is not None and "reset_status" in case:
        assert hit.reset_status == case["reset_status"]


def test_provider_none_matches_any() -> None:
    assert classify_quota("usage_limit_reached") is not None


def test_reset_without_zone_is_not_guessed() -> None:
    hit = classify_quota("Rate limit [x] (resets 2026-10-20T23:50:00)")
    assert hit is not None and hit.until is None
    assert hit.reset_status == "ambiguous"
    assert hit.reset_text == "2026-10-20T23:50:00"


def test_codex_notice_keeps_provider_words_and_no_time() -> None:
    notice = (
        "You\u2019ve hit your usage limit. Upgrade to Plus to continue using Codex, "
        "or try again at Nov 19th, 2026 2:08 PM."
    )
    hit = classify_quota(notice, "openai")
    assert hit is not None and hit.code == "quota_exhausted" and hit.until is None
    assert hit.reset_status == "ambiguous"
    assert hit.reset_text == "Nov 19th, 2026 2:08 PM"
    assert hit.reason.startswith("You\u2019ve hit your usage limit")


def test_absent_reset_is_explicit() -> None:
    hit = classify_quota("usage_limit_reached", "openai")
    assert hit is not None and hit.reset_status == "absent" and hit.reset_text is None


def test_notice_is_sanitized() -> None:
    hit = classify_quota(
        "usage_limit_reached\x00 Bearer abcdefghijklmnop https://x.io/a?token=secret",
        "openai",
    )
    assert hit is not None
    assert "abcdefghijklmnop" not in hit.reason and "secret" not in hit.reason
    assert "\x00" not in hit.reason


@pytest.mark.parametrize(
    ("notice", "reason", "expected"),
    [
        ("You've hit your usage limit", "", "quota_exhausted"),
        ("Error: HTTP 401 token_expired", "", "auth_expired"),
        ("fetch failed: ETIMEDOUT", "", "network_unavailable"),
        ("AssertionError in tests/test_x.py", "", "implementation"),
        ("anything", "runtime_switch_failed", "runtime_switch_failed"),
    ],
)
def test_failure_class(notice: str, reason: str, expected: str) -> None:
    assert failure_class(notice, reason) == expected


def test_clock_reset_is_strictly_after_a_non_utc_now() -> None:
    now = datetime.fromisoformat("2026-10-20T23:30:00-03:00")  # 02:30Z
    hit = classify_quota("You've hit your session limit · resets 2am (UTC)", now=now)
    assert hit is not None and hit.until == datetime(2026, 10, 22, 2, tzinfo=UTC)
