import pytest
from argus_core import copilot_auth
from argus_core.accounts import role_accepts

BODY = "FAKEBODY0123456789"


@pytest.mark.parametrize("prefix", ["github_pat_", "gho_", "ghu_"])
def test_each_accepted_prefix_yields_a_token_free_summary(prefix: str) -> None:
    summary = copilot_auth.validate({"token": prefix + BODY})
    assert (summary.kind, summary.mode) == ("subscription", "token")
    assert BODY not in summary.model_dump_json()


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ({"token": "ghp_" + BODY}, "classic personal access token"),
        ({"token": "sk-" + BODY}, "token must start with one of"),
        ({"token": ""}, "token must be a non-empty string"),
        ({"token": "   "}, "token must be a non-empty string"),
        ({"token": "gho_" + BODY + " x"}, "must not contain whitespace"),
        ({"token": "gho_" + BODY + "\n"}, "must not contain whitespace"),
        ({"token": "gho_" + BODY + "\x00"}, "must not contain whitespace"),
        ({"token": "gho_" + "a" * copilot_auth.MAX_TOKEN_LEN}, "too long"),
        ({"token": 12345}, "token must be a non-empty string"),
        ({}, "token must be a non-empty string"),
        ({"token": "gho_" + BODY, "refresh": "x"}, "remove refresh"),
    ],
)
def test_rejections_name_the_rule_and_never_the_value(doc, needle: str) -> None:
    with pytest.raises(copilot_auth.CopilotAuthError, match=needle) as err:
        copilot_auth.validate(doc)
    assert BODY not in str(err.value)
    assert "sk-" not in str(err.value)


def test_copilot_is_executor_only() -> None:
    runtime = ["copilot"]
    assert role_accepts("executor", "copilot", runtime)
    assert not role_accepts("engine", "copilot", runtime)
    assert not role_accepts("repository", "copilot", runtime)
