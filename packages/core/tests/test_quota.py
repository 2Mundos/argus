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
    hit = classify_quota(case["notice"], case["provider"])
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
