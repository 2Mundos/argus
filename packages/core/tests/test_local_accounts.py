"""Local provider account credential rules."""

import pytest
from argus_core.accounts import (
    LOCAL_AUTH_TOKEN,
    AccountCreate,
    fallback_tier,
    local_lease_settings,
    role_accepts,
    validate_local_base_url,
)


def test_validate_local_base_url_accepts_docker_host() -> None:
    assert (
        validate_local_base_url("http://host.docker.internal:8080")
        == "http://host.docker.internal:8080"
    )


def test_validate_local_base_url_rejects_public_host() -> None:
    with pytest.raises(ValueError, match="host must be"):
        validate_local_base_url("http://10.0.0.5:8080")


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
