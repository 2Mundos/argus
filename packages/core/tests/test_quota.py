import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argus_core.quota import classify_quota

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


def test_provider_none_matches_any() -> None:
    assert classify_quota("usage_limit_reached") is not None


def test_reset_without_zone_is_utc() -> None:
    hit = classify_quota("Rate limit [x] (resets 2026-10-20T23:50:00)")
    assert hit is not None and hit.until == datetime(2026, 10, 20, 23, 50, tzinfo=UTC)


def test_clock_reset_is_strictly_after_a_non_utc_now() -> None:
    now = datetime.fromisoformat("2026-10-20T23:30:00-03:00")  # 02:30Z
    hit = classify_quota("You've hit your session limit · resets 2am (UTC)", now=now)
    assert hit is not None and hit.until == datetime(2026, 10, 22, 2, tzinfo=UTC)
