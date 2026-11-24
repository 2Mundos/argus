from datetime import UTC, datetime

import pytest
from argus_core import google_auth
from argus_core.accounts import ENGINE_PROVIDERS, EXECUTOR_ONLY, role_accepts
from argus_core.tasks import EXECUTOR_PROVIDERS, HARNESS_PROVIDERS, ExecutorModel

NOW = datetime(2026, 11, 19, 18, 0, tzinfo=UTC)
FUTURE = "2026-11-19T19:00:00Z"
PAST = "2026-11-19T17:00:00Z"
FAKE_KEY = "AIzaSyFAKE_TEST_KEY_0000000000000000"
FAKE_ACCESS = "ya29.FAKE_TEST_ACCESS_TOKEN_0000000000"
FAKE_REFRESH = "1//FAKE_TEST_REFRESH_TOKEN_00000000"


def test_api_key_shape_yields_api_key_kind_without_echo() -> None:
    summary = google_auth.validate({"api_key": FAKE_KEY}, now=NOW)
    assert summary == google_auth.GoogleAuthSummary(mode="api_key", kind="api_key")
    assert FAKE_KEY not in summary.model_dump_json()


def test_oauth_native_and_flat_shapes_yield_subscription_kind() -> None:
    native = google_auth.validate(
        {
            "token": {
                "access_token": FAKE_ACCESS,
                "token_type": "Bearer",
                "refresh_token": FAKE_REFRESH,
                "expiry": FUTURE,
                "expires_in": 3600,
                "scope": "openid email",
            },
            "auth_method": "consumer",
            "id_token_email": "operator@example.test",
        },
        now=NOW,
    )
    assert native.mode == "oauth"
    assert native.kind == "subscription"
    assert native.access_expires_at == datetime(2026, 11, 19, 19, 0, tzinfo=UTC)
    assert FAKE_ACCESS not in native.model_dump_json()
    assert FAKE_REFRESH not in native.model_dump_json()
    assert "operator@example.test" not in native.model_dump_json()

    flat = google_auth.validate(
        {
            "access_token": FAKE_ACCESS,
            "refresh_token": FAKE_REFRESH,
            "expiry": FUTURE,
            "id_token_email": "operator@example.test",
        },
        now=NOW,
    )
    assert flat.mode == "oauth"
    assert flat.kind == "subscription"
    assert flat.access_expires_at == datetime(2026, 11, 19, 19, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ({}, "needs one of"),
        ({"modelProvider": "gemini"}, "needs one of"),
        ({"api_key": FAKE_KEY, "access_token": FAKE_ACCESS}, "only one of"),
        ({"api_key": FAKE_KEY, "extra": "x"}, "only the field api_key"),
        ({"api_key": ""}, "non-empty string"),
        ({"api_key": "AIzaSy has space"}, "whitespace"),
        ({"api_key": "a" * (google_auth.MAX_TOKEN_LEN + 1)}, "too long"),
        ({"api_key": FAKE_ACCESS}, "OAuth token"),
        ({"api_key": FAKE_REFRESH}, "OAuth token"),
        ({"token": "not-an-object"}, "JSON object"),
        (
            {"token": {"access_token": FAKE_ACCESS}, "access_token": FAKE_ACCESS},
            "not both",
        ),
        ({"token": {"access_token": FAKE_KEY}}, "Gemini API key"),
        ({"access_token": FAKE_ACCESS, "expiry": PAST}, "expired"),
        ({"access_token": FAKE_ACCESS, "expiry": "not-a-date"}, "ISO-8601"),
        ({"access_token": FAKE_ACCESS, "expiry": "2026-11-19T19:00:00"}, "timezone"),
    ],
)
def test_rejections_name_the_rule_and_never_echo_secrets(
    doc: dict, needle: str
) -> None:
    with pytest.raises(google_auth.GoogleAuthError, match=needle) as err:
        google_auth.validate(doc, now=NOW)
    msg = str(err.value)
    for secret in (FAKE_KEY, FAKE_ACCESS, FAKE_REFRESH):
        assert secret not in msg


def test_google_is_executor_only_with_agy_executor() -> None:
    runtime = ["google"]
    assert role_accepts("executor", "google", runtime)
    assert not role_accepts("engine", "google", runtime)
    assert EXECUTOR_PROVIDERS["agy"] == "google"
    assert "google" in EXECUTOR_ONLY
    assert "google" not in ENGINE_PROVIDERS
    assert "google" not in HARNESS_PROVIDERS.values()
    assert (
        ExecutorModel(executor="agy", model="gemini-4-neon-high").provider == "google"
    )


def test_expired_oauth_is_accepted_when_refresh_token_present_and_helpers_preserve_metadata() -> (
    None
):
    expired_doc = {
        "token": {
            "access_token": FAKE_ACCESS,
            "token_type": "Bearer",
            "refresh_token": FAKE_REFRESH,
            "expiry": PAST,
            "expires_in": 3600,
        },
        "auth_method": "consumer",
        "id_token_email": "operator@example.test",
    }
    summary = google_auth.validate(expired_doc, now=NOW)
    assert summary.mode == "oauth"
    assert summary.kind == "subscription"
    assert google_auth.needs_refresh(expired_doc, now=NOW) is True
    assert google_auth.needs_refresh({"api_key": FAKE_KEY}, now=NOW) is False
    assert (
        google_auth.needs_refresh(
            {"access_token": FAKE_ACCESS, "expiry": PAST}, now=NOW
        )
        is False
    )

    fresh_exp = datetime(2026, 11, 19, 19, 0, tzinfo=UTC)
    merged = google_auth.merge_refreshed_token(
        expired_doc,
        access_token="ya29.NEW_FAKE_ACCESS_TOKEN_000000000",
        expiry=fresh_exp,
        expires_in=3600,
        new_refresh_token="1//ROTATED_FAKE_REFRESH_0000000",
    )
    assert merged["auth_method"] == "consumer"
    assert merged["id_token_email"] == "operator@example.test"
    assert merged["token"]["access_token"] == "ya29.NEW_FAKE_ACCESS_TOKEN_000000000"
    assert merged["token"]["refresh_token"] == "1//ROTATED_FAKE_REFRESH_0000000"
    assert merged["token"]["expiry"] == "2026-11-19T19:00:00Z"
    assert google_auth.needs_refresh(merged, now=NOW) is False
    # Original document was not mutated.
    assert expired_doc["token"]["access_token"] == FAKE_ACCESS
