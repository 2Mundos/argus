"""Engine layers and difficulty routing (docs/specs/engines.spec.md)."""

from datetime import UTC, datetime

from argus_api.engines import catalog, router
from argus_core.difficulty import Difficulty
from argus_core.engines import EngineLayer, RouteRequest

NOW = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
POLICY = {
    "trivial": {"agent": ["composer-2.5"]},
    "implementation": {"claude": ["claude-sonnet-5-5"], "codex": ["gpt-6.1-sol"]},
    "hard_implementation": {"claude": ["claude-opus-5-5"]},
}


def ok(detail: str = "checked") -> dict:
    return {"ok": True, "detail": detail, "at": NOW}


def ready(*ids: str) -> dict:
    return {
        i: {k: ok() for k in ("installed", "authenticated", "validated")} for i in ids
    }


def entry(cat, eid):
    return next(e for e in cat.engines if e.id == eid)


def test_layer_matrix() -> None:
    cat = catalog.build({}, None, None)
    got = {e.id: [lay.layer for lay in e.layers] for e in cat.engines}
    both = [EngineLayer.HARNESS, EngineLayer.EXECUTOR]
    assert got["claude"] == both
    assert got["codex"] == both
    assert got["opencode"] == [EngineLayer.EXECUTOR]
    assert got["cursor-agent"] == [EngineLayer.EXECUTOR]


def test_layers_are_ready_independently() -> None:
    ledger = {
        "claude": {
            "installed": ok(),
            "authenticated": ok(),
            "validated": ok(),
            "layers": {
                "executor": {"validated": {"ok": False, "detail": "no", "at": NOW}}
            },
        }
    }
    claude = entry(catalog.build(ledger, None, None), "claude")
    harness, executor = claude.layers
    assert harness.readiness.validated.ok is True
    assert executor.readiness.validated.ok is False
    # The legacy top-level fields follow the primary (harness) layer.
    assert claude.readiness.validated.ok is True
    assert claude.usable == harness.usable


def route(difficulty, ledger, chains=None):
    cat = catalog.build(ledger, None, None)
    req = RouteRequest(summary="t", difficulty=difficulty)
    return router.route(req, cat, policy=POLICY, executor_chains=chains)


def exec_ledger(*ids: str) -> dict:
    out = ready(*ids)
    for i in ids:
        out[i]["layers"] = {"executor": ready(i)[i]}
    return out


def test_simple_goes_to_the_cheap_executor_and_standard_to_claude() -> None:
    ledger = exec_ledger("cursor-agent", "claude", "codex")
    d = route(Difficulty.SIMPLE, ledger)
    assert d.executor_model == "composer-2.5"
    assert d.difficulty == Difficulty.SIMPLE
    assert "agent" in (d.executor_reason or "")
    s = route(Difficulty.STANDARD, ledger)
    assert "claude" in (s.executor_reason or "")
    assert s.executor_model == "claude-sonnet-5-5"
    h = route(Difficulty.HARD, ledger)
    assert h.executor_model == "claude-opus-5-5"


def test_unavailable_executor_falls_down_the_chain_and_is_named() -> None:
    d = route(Difficulty.STANDARD, exec_ledger("codex"))
    assert "codex" in (d.executor_reason or "")
    assert "claude" in (d.executor_reason or "")  # passed over, with the reason


def test_configured_chain_overrides_default() -> None:
    ledger = exec_ledger("claude", "codex")
    d = route(Difficulty.STANDARD, ledger, {"standard": ("codex", "claude")})
    assert d.executor_model == "gpt-6.1-sol"


def test_executor_chain_setting_is_validated(monkeypatch) -> None:
    import pytest
    from argus_api.config import Settings

    monkeypatch.setenv("ARGUS_DIFFICULTY_EXECUTORS", '{"hard": ["codex"]}')
    assert Settings.from_env().difficulty_executors == {"hard": ("codex",)}
    for bad in ('{"epic": ["codex"]}', '{"hard": []}', '{"hard": ["vim"]}', "[]"):
        monkeypatch.setenv("ARGUS_DIFFICULTY_EXECUTORS", bad)
        with pytest.raises(ValueError):
            Settings.from_env()
