"""Local provider account credential rules."""

import pytest
from argus_core.accounts import (
    LOCAL_AUTH_TOKEN,
    AccountCreate,
    LocalAccountUpdate,
    fallback_tier,
    local_api_endpoint,
    local_credential_problem,
    local_lease_settings,
    local_worker_credential,
    role_accepts,
    validate_local_base_url,
)


def test_validate_local_base_url_accepts_docker_host() -> None:
    assert (
        validate_local_base_url("http://host.docker.internal:8080")
        == "http://host.docker.internal:8080"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8080",
        "http://172.17.0.1:8080",  # the raw bridge IP stays refused
        "http://evil.example:8080",
        "http://127.0.0.1.nip.io:8080",
        "http://user:pw@127.0.0.1:8080",
        "http://127.0.0.1:8080/v1",
        "http://127.0.0.1:8080?x=1",
        "ftp://127.0.0.1:8080",
        "http://127.0.0.1:99999",
    ],
)
def test_validate_local_base_url_rejects_anything_but_a_local_origin(url) -> None:
    with pytest.raises(ValueError):
        validate_local_base_url(url)


def test_api_base_url_is_validated_and_named() -> None:
    base = {"base_url": "http://host.docker.internal:8080"}
    assert (
        local_credential_problem({**base, "api_base_url": "http://127.0.0.1:8080"})
        is None
    )
    assert local_credential_problem({**base, "api_base_url": None}) is None
    problem = local_credential_problem(
        {**base, "api_base_url": "http://172.17.0.1:8080"}
    )
    assert problem is not None and problem.startswith("api_base_url host must be")


@pytest.mark.parametrize(
    ("credential", "in_container", "expected"),
    [
        # Host API with Docker workers: the host side of the bridge forwarder.
        (
            {"base_url": "http://host.docker.internal:8080"},
            False,
            ("http://127.0.0.1:8080", "derived"),
        ),
        # API in a container: it resolves host.docker.internal itself.
        (
            {"base_url": "http://host.docker.internal:8080"},
            True,
            ("http://host.docker.internal:8080", "base_url"),
        ),
        (
            {"base_url": "http://127.0.0.1:11434"},
            False,
            ("http://127.0.0.1:11434", "base_url"),
        ),
        # A stated API endpoint wins everywhere.
        (
            {
                "base_url": "http://host.docker.internal:8080",
                "api_base_url": "http://localhost:9000",
            },
            False,
            ("http://localhost:9000", "api_base_url"),
        ),
        (
            {"base_url": "https://host.docker.internal", "api_base_url": None},
            False,
            ("https://127.0.0.1", "derived"),
        ),
    ],
)
def test_the_api_endpoint_is_chosen_per_runtime(
    credential, in_container, expected
) -> None:
    assert local_api_endpoint(credential, in_container=in_container) == expected


def test_a_lease_credential_carries_only_the_worker_keys() -> None:
    stored = {
        "base_url": "http://host.docker.internal:8080",
        "api_base_url": "http://127.0.0.1:8080",
        "auth_token": LOCAL_AUTH_TOKEN,
        "model": "qwen3",
        "context_tokens": 65536,
    }
    assert local_worker_credential(stored) == {
        "base_url": "http://host.docker.internal:8080",
        "auth_token": LOCAL_AUTH_TOKEN,
        "model": "qwen3",
        "context_tokens": 65536,
    }


def test_the_local_editor_keeps_a_worker_endpoint() -> None:
    with pytest.raises(ValueError, match="base_url cannot be cleared"):
        LocalAccountUpdate.model_validate({"base_url": None})
    with pytest.raises(ValueError, match="at least one field"):
        LocalAccountUpdate.model_validate({})
    reset = LocalAccountUpdate.model_validate({"api_base_url": None, "model": None})
    assert reset.model_fields_set == {"api_base_url", "model"}


def test_local_account_create_requires_credential_document() -> None:
    with pytest.raises(ValueError, match="not a secret_ref"):
        AccountCreate(
            name="strata",
            provider="local",
            secret_ref="sops:strata",
        )


def test_local_account_create_defaults_max_leases() -> None:
    body = AccountCreate(
        name="strata",
        provider="local",
        credential={
            "base_url": "http://127.0.0.1:8080",
            "auth_token": LOCAL_AUTH_TOKEN,
        },
    )
    assert body.max_leases == 1


def test_role_accepts_local_as_engine_and_executor() -> None:
    assert role_accepts("engine", "local", ["local", "anthropic"])
    assert role_accepts("executor", "local", ["local", "cursor"])


def test_local_lease_settings_from_credential() -> None:
    settings = local_lease_settings(
        {
            "base_url": "http://host.docker.internal:8080",
            "auth_token": LOCAL_AUTH_TOKEN,
            "model": "qwen3",
        }
    )
    assert settings is not None
    assert settings.base_url == "http://host.docker.internal:8080"
    assert settings.label == "local"
    assert settings.model == "qwen3"
    assert settings.provider is None
    assert local_lease_settings({"base_url": "http://evil.example:8080"}) is None


def test_fallback_tier_puts_cloud_before_local() -> None:
    assert fallback_tier("anthropic") < fallback_tier("local")
