from datetime import UTC, datetime

import pytest
from argus_core import claude_auth

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OAT = "sk-ant-" + "oat01-" + "x" * 40
API = "sk-ant-" + "api03-" + "y" * 40


def oauth(**over):
    doc = {
        "accessToken": "a" * 30,
        "refreshToken": "r" * 30,
        "expiresAt": int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000),
    }
    doc.update(over)
    return {"claudeAiOauth": doc}


@pytest.mark.parametrize(
    ("doc", "mode", "kind"),
    [
        ({"api_key": API}, "api_key", "api_key"),
        ({"oauth_token": OAT}, "oauth_token", "subscription"),
        (oauth(), "claude_ai_oauth", "subscription"),
        (oauth(expiresAt="2026-06-01T00:00:00Z"), "claude_ai_oauth", "subscription"),
        (oauth(expiresAt=None), "claude_ai_oauth", "subscription"),
    ],
)
def test_accepted_shapes_derive_the_kind(doc, mode, kind) -> None:
    summary = claude_auth.validate(doc, now=NOW)
    assert (summary.mode, summary.kind) == (mode, kind)


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({}, "needs one of"),
        ({"token": OAT}, "needs one of"),  # an unknown type is not authenticated
        ({"api_key": API, "oauth_token": OAT}, "only one of"),
        ({"api_key": ""}, "api_key must be"),
        ({"api_key": 5}, "api_key must be"),
        ({"api_key": "has space"}, "whitespace"),
        ({"api_key": OAT}, "use oauth_token"),
        ({"oauth_token": API}, "use api_key"),
        ({"oauth_token": "x" * 5000}, "too long"),
        ({"claudeAiOauth": "nope"}, "must be an object"),
        (oauth(refreshToken=""), "missing: refreshToken"),
        ({"claudeAiOauth": {}}, "missing: accessToken, refreshToken"),
        (oauth(expiresAt="soon"), "expiresAt is not a date"),
        (oauth(expiresAt=True), "expiresAt is not a date"),
        (oauth(expiresAt=1000), "expiresAt is in the past"),
    ],
)
def test_rejections_name_fields_and_never_values(doc, message) -> None:
    with pytest.raises(claude_auth.ClaudeAuthError) as err:
        claude_auth.validate(doc, now=NOW)
    assert message in str(err.value)
    for secret in (OAT, API, "a" * 30, "r" * 30, "has space"):
        assert secret not in str(err.value)
