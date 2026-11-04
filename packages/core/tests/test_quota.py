import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argus_core.quota import _reset, classify_quota, failure_class, sanitize_notice

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
        ("Error: stream disconnected before completion", "", "network_unavailable"),
        ("Reconnecting... 2/5: stream disconnected", "", "network_unavailable"),
        ("Error: connect ECONNREFUSED 10.0.0.1:443", "", "network_unavailable"),
        ("task workspace failed: TypeError: fetch failed", "", "network_unavailable"),
        ("Request timed out", "", "network_unavailable"),
        ("AssertionError in tests/test_x.py", "", "implementation"),
        # The work's own words are not network evidence (failure evidence audit).
        ("Tests timed out after 30s", "", "implementation"),
        (
            "pytest could not reach the test database: connection refused",
            "",
            "implementation",
        ),
        ("Timed out waiting for the dev server", "", "implementation"),
        (
            "the agent run failed (log: npm ERR! fetch failed | exit 1)",
            "",
            "implementation",
        ),
        ("build failed: connection reset by peer", "", "implementation"),
        ("anything", "runtime_switch_failed", "runtime_switch_failed"),
    ],
)
def test_failure_class(notice: str, reason: str, expected: str) -> None:
    assert failure_class(notice, reason) == expected


def test_clock_reset_is_strictly_after_a_non_utc_now() -> None:
    now = datetime.fromisoformat("2026-10-20T23:30:00-03:00")  # 02:30Z
    hit = classify_quota("You've hit your session limit · resets 2am (UTC)", now=now)
    assert hit is not None and hit.until == datetime(2026, 10, 22, 2, tzinfo=UTC)


def test_zoned_iso_with_fraction_and_offset_still_parses() -> None:
    raw = "2026-10-23T15:00:00.123456+00:00"
    hit = classify_quota(f"Rate limit [x] (resets {raw})", "anthropic")
    assert hit is not None and hit.until == datetime.fromisoformat(raw)
    bare = classify_quota("Rate limit [x] (resets 2026-10-20T23:50:00)", "anthropic")
    assert (
        bare is not None
        and bare.until is None
        and bare.reset_text == "2026-10-20T23:50:00"
    )


def test_sanitize_notice_strips_queries_and_bounds_a_run_of_http_prefixes() -> None:
    assert (
        sanitize_notice("see https://a.io/p?token=zzz#f more")
        == "see https://a.io/p more"
    )
    # A marker with no path character before it is not a query suffix.
    assert "token" in sanitize_notice("http://?token=zzz")
    assert sanitize_notice("(https://a.io/p?token=zzz)") == "(https://a.io/p)"
    notice = "Bearer abcdefghijkl https://a.io/p?token=zzz " + ("http://" * 8000)
    started = time.perf_counter()
    out = sanitize_notice(notice)
    # check-tests: allow elapsed-time — ReDoS regression for repeated http:// without a query
    assert time.perf_counter() - started < 0.5
    assert len(out) <= 500
    assert "abcdefghijkl" not in out and "zzz" not in out and "?" not in out
    assert out.startswith("[redacted] https://a.io/p")


def test_unclosed_resets_markers_do_not_backtrack() -> None:
    body = "(resets " * 8000
    now = datetime(2026, 1, 1, tzinfo=UTC)
    started = time.perf_counter()
    assert _reset(body, now) is None
    # check-tests: allow elapsed-time — ReDoS regression for unclosed "(resets" repeats
    assert time.perf_counter() - started < 0.5
    stamped = "(resets " * 100 + "(resets 2026-10-20T23:50:00Z)"
    assert _reset(stamped, now) == datetime(2026, 10, 20, 23, 50, tzinfo=UTC)
