import json
from pathlib import Path

import pytest

from evals import runner
from evals.model import ToolSpec, Turn
from evals.runner import EvalSpec, format_summary, run_eval, summarize
from evals.tests.fakes import ScriptedClient, call

TOOLS = [
    ToolSpec("get_inbox", "Inbox.", {"type": "object", "properties": {}}),
    ToolSpec("cancel_task", "Cancel.", {"type": "object", "properties": {}}),
]


def spec(tmp_path: Path, cases: dict[str, dict]) -> EvalSpec:
    (tmp_path / "cases").mkdir()
    for name, data in cases.items():
        (tmp_path / "cases" / f"{name}.json").write_text(json.dumps(data))
    return EvalSpec("demo", tmp_path, "SYSTEM", TOOLS)


def case(cid: str, **checks) -> dict:
    return {
        "id": cid,
        "source": "task:placeholder",
        "prompt": "go",
        "world": [{"tool": "get_inbox", "result": {"items": []}}],
        "max_turns": 3,
        "checks": checks,
    }


GOOD = [
    Turn(calls=[call("get_inbox")], cost_usd=0.01),
    Turn(text="done", cost_usd=0.01),
]
BAD = [
    Turn(calls=[call("cancel_task")], cost_usd=0.01),
    Turn(text="done", cost_usd=0.01),
]


def test_samples_cost_and_system_prompt(tmp_path):
    s = spec(tmp_path, {"a": case("a", expect=[{"tool": "get_inbox"}])})
    client = ScriptedClient(GOOD * 2)
    rows = run_eval(s, client, samples=2)
    assert [r["passed"] for r in rows] == [True, True]
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(0.04)
    assert set(client.systems) == {"SYSTEM"}


def test_a_model_that_never_stops_fails_the_sample(tmp_path):
    s = spec(tmp_path, {"a": case("a")})
    rows = run_eval(s, ScriptedClient([Turn(calls=[call("get_inbox")])] * 3), samples=1)
    assert rows[0]["failures"] == ["did not stop within 3 turns"]


def test_half_failing_samples_fail_the_case(tmp_path):
    s = spec(tmp_path, {"a": case("a", forbid=[{"tool": "cancel_task"}])})
    rows = run_eval(s, ScriptedClient(GOOD + BAD), samples=2)
    summary = summarize(s, rows, model="m", samples=2)
    assert summary["failed_cases"] == ["a"]
    assert summary["cases"]["a"]["failed_samples"] == 1
    assert "FAIL a" in format_summary(summary)


def test_only_selected_cases_run_and_unknown_is_an_error(tmp_path):
    s = spec(tmp_path, {"a": case("a"), "b": case("b")})
    rows = run_eval(s, ScriptedClient([Turn(text="ok")]), samples=1, only=["b"])
    assert [r["case"] for r in rows] == ["b"]
    with pytest.raises(ValueError, match="unknown case: zzz"):
        run_eval(s, ScriptedClient([]), samples=1, only=["zzz"])


def test_rubric_is_graded_by_the_client_and_costed(tmp_path):
    s = spec(tmp_path, {"a": case("a", rubric="Says it stopped.")})
    verdicts = [Turn(text="PASS ok", cost_usd=0.5), Turn(text="FAIL no", cost_usd=0.5)]
    rows = run_eval(
        s,
        ScriptedClient([Turn(text="x"), verdicts[0], Turn(text="x"), verdicts[1]]),
        samples=2,
    )
    assert [r["passed"] for r in rows] == [True, False]
    assert rows[1]["failures"] == ["rubric: FAIL no"]
    assert rows[0]["cost_usd"] == 0.5


def test_baseline_comparison_reports_regressions_and_fixes(tmp_path):
    s = spec(
        tmp_path,
        {
            "a": case("a", forbid=[{"tool": "cancel_task"}]),
            "b": case("b", forbid=[{"tool": "cancel_task"}]),
        },
    )
    base = summarize(
        s, run_eval(s, ScriptedClient([*GOOD, *BAD]), samples=1), model="m", samples=1
    )
    new = summarize(
        s,
        run_eval(s, ScriptedClient([*BAD, *GOOD]), samples=1),
        model="m",
        samples=1,
        baseline=base,
    )
    assert new["baseline"]["regressed"] == ["a"]
    assert new["baseline"]["fixed"] == ["b"]


def test_main_writes_rows_and_summary_and_checks_baseline_model(tmp_path, monkeypatch):
    s = spec(tmp_path, {"a": case("a", expect=[{"tool": "get_inbox"}])})
    monkeypatch.setattr(
        runner, "resolve_client", lambda target: lambda model: ScriptedClient(GOOD * 4)
    )
    argv = ["--model", "m1", "--client", "x:y", "--samples", "2"]
    assert runner.main(s, ["--out", "first", *argv]) == 0
    out = tmp_path / "runs" / "first"
    assert len((out / "rows.jsonl").read_text().splitlines()) == 2
    assert json.loads((out / "summary.json").read_text())["cost_usd"] == pytest.approx(
        0.04
    )
    assert runner.main(s, ["--out", "second", "--baseline", "first", *argv]) == 0
    with pytest.raises(SystemExit, match="compare the same model"):
        runner.main(
            s,
            [
                "--out",
                "third",
                "--baseline",
                "first",
                "--model",
                "m2",
                "--client",
                "x:y",
            ],
        )
    with pytest.raises(SystemExit):
        runner.main(s, ["--out", "../escape", *argv])
