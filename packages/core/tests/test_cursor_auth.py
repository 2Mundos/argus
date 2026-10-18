import base64
import json
from datetime import UTC, datetime

import pytest
from argus_core import cursor_auth

NOW = datetime(2026, 1, 1, tzinfo=UTC)
FUTURE = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp())
PAST = int(datetime(2025, 6, 1, tzinfo=UTC).timestamp())


def jwt(exp: int | None = FUTURE, **claims) -> str:
    """A fake, unsigned JWT-shaped token."""

    def b(o) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    payload = {"sub": "auth0|fake", **claims, **({"exp": exp} if exp else {})}
    return f"{b({'alg': 'none'})}.{b(payload)}.fakesig"


REFRESH = jwt(FUTURE + 1)
API_KEY = "key_" + "0" * 40


def login(**over):
    return {"accessToken": jwt(), "refreshToken": REFRESH, **over}


@pytest.mark.parametrize(
    ("doc", "mode", "kind"),
    [
        ({"api_key": API_KEY}, "api_key", "api_key"),
        (login(), "oauth", "subscription"),
        # CLI extras that ride along in a login export are tolerated.
        (login(authId="auth0|fake", email="a@b.c"), "oauth", "subscription"),
    ],
)
def test_accepted_shapes_derive_the_kind(doc, mode, kind) -> None:
    summary = cursor_auth.validate(doc, now=NOW)
    assert (summary.mode, summary.kind) == (mode, kind)


def test_oauth_reports_the_access_token_expiry_only() -> None:
    summary = cursor_auth.validate(login(), now=NOW)
    assert summary.access_expires_at == datetime.fromtimestamp(FUTURE, UTC)
    assert cursor_auth.validate({"api_key": API_KEY}, now=NOW).access_expires_at is None


SETTINGS_ONLY = {
    "version": 1,
    "permissions": {"allow": [], "deny": []},
    "editor": {"vimMode": False},
    "model": {"modelId": "composer-2.5"},
    "authInfo": {"authId": "auth0|fake", "email": "a@b.c"},
}


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({}, "needs one of"),
        (SETTINGS_ONLY, "a Cursor settings file is not a credential"),
        ({"token": API_KEY}, "needs one of"),
        ({"api_key": API_KEY, "accessToken": jwt()}, "only one of"),
        ({"api_key": ""}, "api_key must be"),
        ({"api_key": 5}, "api_key must be"),
        ({"api_key": "has space"}, "whitespace"),
        ({"api_key": "x" * 9000}, "too long"),
        ({"api_key": jwt()}, "put it in accessToken"),
        ({"accessToken": jwt()}, "missing: refreshToken"),
        ({"refreshToken": REFRESH, "accessToken": ""}, "missing: accessToken"),
        ({"accessToken": "", "refreshToken": ""}, "missing: accessToken, refreshToken"),
        (login(accessToken="not-a-jwt"), "not a JWT with an exp claim"),
        (login(accessToken=jwt(exp=None)), "not a JWT with an exp claim"),
        (login(accessToken=jwt(PAST)), "accessToken is expired"),
        (login(refreshToken="has space"), "whitespace"),
        (login(refreshToken=5), "missing: refreshToken"),
    ],
)
def test_rejections_name_fields_and_never_values(doc, message) -> None:
    with pytest.raises(cursor_auth.CursorAuthError) as err:
        cursor_auth.validate(doc, now=NOW)
    assert message in str(err.value)
    text = str(err.value)
    for secret in (API_KEY, jwt(), jwt(PAST), REFRESH, "has space", "not-a-jwt"):
        assert secret not in text
    assert "a@b.c" not in text and "composer" not in text


def test_an_expired_refresh_token_does_not_matter_only_the_access_token_is_used() -> (
    None
):
    summary = cursor_auth.validate(login(refreshToken=jwt(PAST)), now=NOW)
    assert summary.mode == "oauth"
