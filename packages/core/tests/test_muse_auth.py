import pytest
from argus_core import muse_auth
from argus_core.accounts import ENGINE_PROVIDERS, EXECUTOR_ONLY, role_accepts
from argus_core.tasks import EXECUTOR_PROVIDERS, HARNESS_PROVIDERS, ExecutorModel

BODY = "FAKEBODY0123456789"


def test_each_shape_yields_a_token_free_summary() -> None:
    assert muse_auth.validate({"api_key": "meta-" + BODY}).model_dump() == {
        "mode": "api_key",
        "kind": "api_key",
    }
    assert muse_auth.validate({"oauth_token": "oauth-" + BODY}).model_dump() == {
        "mode": "oauth_token",
        "kind": "subscription",
    }
    native = {
        "providers": {"meta": {"mechanism": "oauth", "access_token": "tok-" + BODY}}
    }
    summary = muse_auth.validate(native)
    assert (summary.kind, summary.mode) == ("subscription", "oauth_token")
    assert BODY not in summary.model_dump_json()


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ({"api_key": ""}, "api_key must be a non-empty string"),
        ({"api_key": "   "}, "api_key must be a non-empty string"),
        ({"api_key": "meta-" + BODY + " x"}, "must not contain whitespace"),
        ({"api_key": "meta-" + BODY, "oauth_token": "x"}, "give only one of"),
        (
            {"oauth_token": "meta-" + BODY, "other": "x"},
            "only the field oauth_token is allowed",
        ),
        ({"oauth_token": 12345}, "oauth_token must be a non-empty string"),
        ({}, "needs one of"),
        ({"other": "x"}, "needs one of"),
        ({"providers": {"meta": {}}}, "needs one of"),
    ],
)
def test_rejections_name_the_rule_and_never_the_value(doc, needle: str) -> None:
    with pytest.raises(muse_auth.MuseAuthError, match=needle) as err:
        muse_auth.validate(doc)
    assert BODY not in str(err.value)


def test_muse_is_harness_and_executor_on_its_own_provider() -> None:
    assert "muse" in ENGINE_PROVIDERS
    assert "muse" not in EXECUTOR_ONLY
    assert HARNESS_PROVIDERS["muse"] == "muse"
    assert EXECUTOR_PROVIDERS["muse"] == "muse"
    runtime = ["muse"]
    assert role_accepts("engine", "muse", runtime)
    assert role_accepts("executor", "muse", runtime)
    assert not role_accepts("repository", "muse", runtime)
    assert ExecutorModel(executor="muse", model="muse-spark").provider == "muse"
