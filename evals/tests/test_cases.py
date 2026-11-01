"""The shipped cases stay valid against the real tool list."""

import importlib.util
from pathlib import Path

import pytest

from evals.cases import load_cases
from evals.runner import EvalSpec

ROOT = Path(__file__).resolve().parents[1]
EVALS = ["orchestrator-tools", "worker-protocol"]


def build(name: str) -> EvalSpec:
    path = ROOT / name / "run.py"
    mod_spec = importlib.util.spec_from_file_location(f"run_{name}", path)
    assert mod_spec and mod_spec.loader
    module = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(module)
    return module.build()


@pytest.mark.parametrize("name", EVALS)
def test_cases_are_well_formed_and_name_real_tools(name):
    spec = build(name)
    known = {t.name for t in spec.tools}
    cases = load_cases(spec.directory / "cases")
    assert len({c.id for c in cases}) == len(cases) >= 4
    assert any(c.control for c in cases)
    for c in cases:
        assert c.source
        used = {r["tool"] for r in c.world}
        used |= {p.tool for p in [*c.checks.expect, *c.checks.forbid]}
        assert used <= known, f"{c.id}: not served to this role: {used - known}"
        assert c.checks.expect or c.checks.forbid or c.checks.rubric


def test_text_under_test_is_imported_from_the_source_modules():
    from argus_api import mcp_server

    orchestrator = build("orchestrator-tools")
    worker = build("worker-protocol")
    assert mcp_server.ORCHESTRATOR_SKILL in orchestrator.system
    assert worker.system == mcp_server.INSTRUCTIONS
    assert {t.name: t.description for t in worker.tools}["claim_task"] == (
        mcp_server.claim_task.__doc__
    )
