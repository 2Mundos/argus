"""Complete alternatives and per-difficulty inheritance, without runtime side effects."""

from copy import deepcopy

import pytest
from argus_core.execution_chains import ExecutionChains, resolve_chains
from pydantic import ValidationError

LEVELS = ("trivial", "simple", "standard", "hard")


def single(id="direct", provider="local", adapter="claude", model=None):
    return {
        "id": id,
        "mode": "single",
        "runtime": "nanoclaw",
        "harness": {
            "adapter": adapter,
            "provider": provider,
            "account_id": "account-one",
            "model": model or {"source": "account"},
        },
    }


def paired():
    return {
        **single("pair"),
        "mode": "paired",
        "executor": {
            "adapter": "opencode",
            "provider": "custom",
            "account_id": "account-two",
            "model": {"source": "explicit", "id": "vendor/code"},
        },
    }


def policy(level, alternatives):
    return ExecutionChains.model_validate(
        {"version": 1, "difficulties": {level: alternatives}}
    )


@pytest.mark.parametrize("difficulty", LEVELS)
@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_complete_alternatives_roundtrip(difficulty, reverse):
    entries = [paired(), single()]
    if reverse:
        entries.reverse()
    value = {"version": 1, "difficulties": {difficulty: entries}}
    parsed = ExecutionChains.model_validate(value)
    assert parsed.model_dump(mode="json") == value
    assert ExecutionChains.model_validate_json(parsed.model_dump_json()) == parsed
    direct = next(a for a in parsed.difficulties[difficulty] if a.mode == "single")
    assert "executor" not in direct.model_dump(mode="json")


@pytest.mark.parametrize(
    "change",
    [
        {"executor": None},
        {"executor": paired()["executor"]},
        {"runtime": "builtin"},
        {"mode": "agent"},
        {"extra": True},
        {"id": " route "},
        {"harness": {**single()["harness"], "adapter": "opencode"}},
        {"harness": {**single()["harness"], "provider": "cursor"}},
        {"harness": {**single()["harness"], "account_id": ""}},
        {"harness": {**single()["harness"], "url": "https://example.invalid"}},
        {
            "harness": {
                **single()["harness"],
                "model": {"source": "explicit", "id": "other"},
            }
        },
    ],
)
def test_reject_unsupported_or_open_contract(change):
    with pytest.raises(ValidationError):
        policy("hard", [{**single(), **change}])


@pytest.mark.parametrize(
    "entries", [[], [single(), single()], [single(str(n)) for n in range(17)]]
)
def test_chain_bounds_and_unique_ids(entries):
    with pytest.raises(ValidationError):
        policy("simple", entries)


def test_sixteen_distinct_routes_and_same_id_in_other_difficulty():
    entries = [single(str(n)) for n in range(16)]
    assert len(policy("simple", entries).difficulties["simple"]) == 16
    assert (
        len(
            ExecutionChains.model_validate(
                {
                    "version": 1,
                    "difficulties": {"trivial": [single()], "hard": [single()]},
                }
            ).difficulties
        )
        == 2
    )


@pytest.mark.parametrize(
    "value",
    [
        {"version": 2, "difficulties": {}},
        {"version": 1, "difficulties": {"unknown": [single()]}},
        {"version": 1, "difficulties": {}, "fallback": []},
    ],
)
def test_policy_is_closed_and_versioned(value):
    with pytest.raises(ValidationError):
        ExecutionChains.model_validate(value)


@pytest.mark.parametrize(
    "provider,adapter", [("anthropic", "claude"), ("openai", "codex")]
)
def test_native_single_requires_explicit_model(provider, adapter):
    route = single(provider=provider, adapter=adapter)
    with pytest.raises(ValidationError, match="unsupported_harness_selector"):
        policy("standard", [route])
    route["harness"]["model"] = {"source": "explicit", "id": "vendor/model-1"}
    assert (
        policy("standard", [route]).difficulties["standard"][0].harness.provider
        == provider
    )


@pytest.mark.parametrize("model", ["", " /invalid", "x" * 201, "model?key=value"])
def test_model_ids_are_not_repaired(model):
    route = single(provider="anthropic", model={"source": "explicit", "id": model})
    with pytest.raises(ValidationError):
        policy("standard", [route])


def test_local_redirect_guard_and_executor_only_adapters():
    route = paired()
    route["executor"].update(adapter="claude", provider="anthropic")
    with pytest.raises(ValidationError, match="local_pair_restriction"):
        policy("trivial", [route])
    for adapter, provider in [
        ("opencode", "custom"),
        ("agent", "cursor"),
        ("copilot", "copilot"),
    ]:
        route = single(adapter=adapter, provider=provider)
        with pytest.raises(ValidationError, match="unsupported_harness_selector"):
            policy("hard", [route])


def test_custom_harness_does_not_inherit_local_only_pair_restrictions():
    route = paired()
    route["harness"]["provider"] = "custom"
    route["executor"].update(adapter="claude", provider="anthropic")
    assert policy("hard", [route]).difficulties["hard"][0].executor.adapter == "claude"


def test_pair_requires_executor_and_valid_role_provider():
    route = single()
    route["mode"] = "paired"
    with pytest.raises(ValidationError):
        policy("simple", [route])
    route["executor"] = {**paired()["executor"], "provider": "anthropic"}
    with pytest.raises(ValidationError, match="unsupported_executor_selector"):
        policy("simple", [route])


def test_same_native_provider_cannot_select_two_accounts_for_one_gateway_lane():
    route = single(provider="anthropic", model={"source": "explicit", "id": "harness"})
    route.update(
        mode="paired", executor={**route["harness"], "account_id": "different"}
    )
    with pytest.raises(ValidationError, match="one_gateway_account_per_provider"):
        policy("standard", [route])
    route["executor"]["account_id"] = route["harness"]["account_id"]
    assert policy("standard", [route]).difficulties["standard"][0].mode == "paired"


def test_account_is_an_optional_pin_and_unpinned_roundtrips_without_it():
    route = single()
    del route["harness"]["account_id"]
    value = {"version": 1, "difficulties": {"trivial": [route]}}
    parsed = ExecutionChains.model_validate(value)
    assert parsed.difficulties["trivial"][0].harness.account_id is None
    assert parsed.model_dump(mode="json") == value
    explicit_null = deepcopy(value)
    explicit_null["difficulties"]["trivial"][0]["harness"]["account_id"] = None
    assert ExecutionChains.model_validate(explicit_null) == parsed
    pinned = policy("trivial", [single()])
    assert (
        resolve_chains(parsed)["trivial"].revision
        != resolve_chains(pinned)["trivial"].revision
    )


def test_unpinned_native_pair_defers_the_same_account_rule_to_selection():
    route = single(provider="anthropic", model={"source": "explicit", "id": "harness"})
    route.update(mode="paired", executor={**route["harness"]})
    del route["executor"]["account_id"]
    assert policy("standard", [route]).difficulties["standard"][0].mode == "paired"
    del route["harness"]["account_id"]
    assert policy("standard", [route]).difficulties["standard"][0].mode == "paired"


@pytest.mark.parametrize(
    "adapter,provider,source",
    [
        ("claude", "anthropic", "explicit"),
        ("codex", "openai", "explicit"),
        ("agent", "cursor", "explicit"),
        ("copilot", "copilot", "explicit"),
        ("opencode", "local", "account"),
        ("opencode", "custom", "account"),
        ("opencode", "custom", "explicit"),
    ],
)
def test_executor_selector_matrix_is_independent_of_harness_model(
    adapter, provider, source
):
    route = single(provider="custom")
    route["mode"] = "paired"
    route["executor"] = {
        "adapter": adapter,
        "provider": provider,
        "account_id": "account-two",
        "model": {
            "source": source,
            **({"id": "vendor/code"} if source == "explicit" else {}),
        },
    }
    parsed = policy("hard", [route]).difficulties["hard"][0]
    assert parsed.model_dump(mode="json") == route


def test_custom_same_account_selectors_still_preserve_both_roles():
    route = paired()
    route["harness"]["provider"] = "custom"
    route["executor"]["account_id"] = route["harness"]["account_id"]
    parsed = policy("hard", [route]).difficulties["hard"][0]
    assert parsed.harness.account_id == parsed.executor.account_id
    assert parsed.harness.model.source == "account"
    assert parsed.executor.model.source == "explicit"


@pytest.mark.parametrize("difficulty", LEVELS)
def test_project_replaces_whole_chain_and_reset_inherits(difficulty):
    global_policy = policy(difficulty, [paired(), single()])
    project_policy = policy(difficulty, [single("override")])
    result = resolve_chains(global_policy, project_policy)
    assert result[difficulty].source == "project"
    assert [a.id for a in result[difficulty].alternatives] == ["override"]
    inherited = resolve_chains(global_policy, ExecutionChains())
    assert inherited[difficulty].source == "global"
    assert [a.id for a in inherited[difficulty].alternatives] == ["pair", "direct"]
    assert inherited[difficulty] == resolve_chains(global_policy)[difficulty]
    assert all(result[d].source == "legacy" for d in LEVELS if d != difficulty)


def test_legacy_has_no_implicit_routes_and_inheritance_does_not_mutate_inputs():
    assert all(
        r.source == "legacy" and r.alternatives is None
        for r in resolve_chains().values()
    )
    global_policy = policy("trivial", [single()])
    before = deepcopy(global_policy)
    result = resolve_chains(global_policy, policy("hard", [paired()]))
    result["trivial"].alternatives[0].id = "changed"
    assert global_policy == before
    assert resolve_chains(global_policy)["trivial"].alternatives[0].id == "direct"


def test_revision_is_deterministic_per_difficulty_and_tracks_order_and_source():
    base = policy("hard", [paired(), single()])
    route = resolve_chains(base)["hard"]
    assert (
        resolve_chains(ExecutionChains.model_validate_json(base.model_dump_json()))[
            "hard"
        ]
        == route
    )
    unrelated = base.model_copy(deep=True)
    unrelated.difficulties["simple"] = policy("simple", [single()]).difficulties[
        "simple"
    ]
    assert resolve_chains(unrelated)["hard"].revision == route.revision
    reordered = policy("hard", [single(), paired()])
    assert resolve_chains(reordered)["hard"].revision != route.revision
    assert resolve_chains(base, base)["hard"].revision != route.revision
