"""Custom OpenAI-compatible accounts: parser parity with Radar, config rules, the
request body and the 402/429 quota notice (custom accounts spec)."""

from datetime import UTC, datetime

import pytest
from argus_core.accounts import AccountCreate
from argus_core.custom_accounts import (
    CustomAccountError,
    CustomConfig,
    RoutingOptions,
    chat_body,
    check_base_url,
    lease_settings,
    parse_endpoints,
    validate_credential,
)
from argus_core.custom_accounts import (
    openrouter_provider_block_from_preferences as block,
)
from argus_core.quota import classify_quota, quota_notice_from_response
from pydantic import ValidationError

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OR = "https://openrouter.ai/api/v1"


def test_block_accepts_lists_and_comma_strings_and_drops_empties() -> None:
    assert block({"order": ["a", " b ", ""], "quantizations": "fp8, ,bf16"}) == {
        "order": ["a", "b"],
        "quantizations": ["fp8", "bf16"],
    }
    assert block({"only": "x,,y", "allow_fallbacks": True}) == {
        "only": ["x", "y"],
        "allow_fallbacks": True,
    }


def test_no_fallbacks_with_order_pins_only() -> None:
    assert block({"order": ["a", "b"], "allow_fallbacks": False}) == {
        "order": ["a", "b"],
        "only": ["a", "b"],
        "allow_fallbacks": False,
    }
    assert block({"order": ["a"], "only": ["z"], "allow_fallbacks": False})["only"] == [  # type: ignore[index]
        "z"
    ]
    assert block({"allow_fallbacks": False}) == {"allow_fallbacks": False}


@pytest.mark.parametrize("prefs", [None, {}, {"order": []}, {"order": " , "}])
def test_empty_preferences_are_none(prefs) -> None:
    assert block(prefs) is None


def test_routing_options_model_matches_the_parser() -> None:
    options = RoutingOptions(order="a,b", allow_fallbacks=False)
    assert options.provider_block() == block({"order": "a,b", "allow_fallbacks": False})
    with pytest.raises(ValidationError):
        RoutingOptions(order=[1])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        RoutingOptions(unknown=1)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x/api",
        "openrouter.ai/api/v1",
        "https://user:pw@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?key=sk-secret",
        "https://openrouter.ai/api/v1#frag",
    ],
)
def test_bad_base_urls_are_rejected_without_echoing_them_in_the_message(
    url: str,
) -> None:
    with pytest.raises(ValidationError) as err:
        CustomConfig(base_url=url, label="openrouter")
    assert all(
        "sk-secret" not in e["msg"] and "pw@" not in e["msg"]
        for e in err.value.errors()
    )


def test_check_base_url_strips_the_trailing_slash() -> None:
    assert check_base_url(OR + "/") == OR
    with pytest.raises(ValueError, match="LOCAL"):
        check_base_url("file:///x", "LOCAL")


def test_routing_options_need_the_openrouter_label() -> None:
    with pytest.raises(ValidationError, match="openrouter"):
        CustomConfig(
            base_url="http://localhost:8000/v1",
            label="vllm",
            executor={"routing_options": {"order": ["a"]}},  # type: ignore[arg-type]
        )
    ok = CustomConfig(base_url=OR, label="openrouter", executor={"model": "a/b"})  # type: ignore[arg-type]
    assert ok.host == "openrouter.ai"


@pytest.mark.parametrize("model", ["has space", "-lead", "", "x" * 201])
def test_model_ids_are_opaque_but_bounded(model: str) -> None:
    with pytest.raises(ValidationError):
        CustomConfig(base_url=OR, label="openrouter", engine={"model": model})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "credential",
    [
        {},
        {"api_key": ""},
        {"api_key": " k"},
        {"api_key": "a\nb"},
        {"api_key": "k", "x": 1},
    ],
)
def test_credential_shape(credential: dict) -> None:
    with pytest.raises(CustomAccountError):
        validate_credential(credential)
    validate_credential({"api_key": "sk-or-fake"})


def test_account_create_needs_config_for_custom_only() -> None:
    cfg = {"base_url": OR, "label": "openrouter"}
    AccountCreate(name="c", provider="custom", credential={"api_key": "k"}, config=cfg)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AccountCreate(name="c", provider="custom", credential={"api_key": "k"})
    with pytest.raises(ValidationError):
        AccountCreate(
            name="c", provider="anthropic", credential={"api_key": "k"}, config=cfg
        )  # type: ignore[arg-type]


def test_lease_settings_and_chat_body_per_role() -> None:
    config = CustomConfig(
        base_url=OR,
        label="openrouter",
        engine={"model": "a/engine"},  # type: ignore[arg-type]
        executor={  # type: ignore[arg-type]
            "model": "a/exec",
            "routing_options": {"order": "deepinfra", "allow_fallbacks": False},
        },
    )
    engine, executor = (
        lease_settings(config, "engine"),
        lease_settings(config, "executor"),
    )
    assert (engine.model, engine.provider) == ("a/engine", None)
    msgs = [{"role": "user", "content": "hi"}]
    assert chat_body(engine, msgs) == {"model": "a/engine", "messages": msgs}
    assert chat_body(executor, msgs, stream=False) == {
        "model": "a/exec",
        "messages": msgs,
        "stream": False,
        "provider": {
            "order": ["deepinfra"],
            "only": ["deepinfra"],
            "allow_fallbacks": False,
        },
    }
    with pytest.raises(CustomAccountError):
        chat_body(lease_settings(CustomConfig(base_url=OR, label="x"), "engine"), msgs)


def test_parse_endpoints_keeps_only_well_formed_entries() -> None:
    parsed = parse_endpoints(
        "a/b",
        {
            "data": {
                "endpoints": [
                    {
                        "provider_name": "DeepInfra",
                        "quantization": "fp8",
                        "context_length": 65536,
                        "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
                        "status": 0,
                    },
                    {"quantization": "x"},
                    "junk",
                ]
            }
        },
    )
    [e] = parsed.endpoints
    assert (e.provider_name, e.quantization, e.context_length) == (
        "DeepInfra",
        "fp8",
        65536,
    )
    assert (e.prompt_price, e.status) == ("0.0000001", "0")
    with pytest.raises(CustomAccountError):
        parse_endpoints("a/b", {"data": {}})


def test_quota_notice_from_402_and_429() -> None:
    notice = quota_notice_from_response(429, {"Retry-After": "120"}, NOW)
    assert notice is not None
    hit = classify_quota(notice, "custom", NOW)
    assert hit is not None and hit.until == datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    hit = classify_quota(quota_notice_from_response(402, {}, NOW), "custom", NOW)  # type: ignore[arg-type]
    assert hit is not None and hit.until is None
    reset = quota_notice_from_response(
        429, {"X-RateLimit-Reset": str(int(NOW.timestamp() + 3600) * 1000)}, NOW
    )
    assert classify_quota(reset, "custom", NOW).until == datetime(  # type: ignore[arg-type, union-attr]
        2026, 1, 1, 1, tzinfo=UTC
    )


@pytest.mark.parametrize("status", [200, 400, 401, 500])
def test_other_statuses_are_not_quota(status: int) -> None:
    assert quota_notice_from_response(status, {}, NOW) is None
