"""Run an eval: samples per case against a fake world, graded, with cost."""

import argparse
import importlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.cases import Case, load_cases
from evals.grade import case_failed, grade
from evals.model import (
    ClientFactory,
    Event,
    ModelClient,
    ToolCall,
    ToolSpec,
    UserMessage,
)
from evals.world import World

GRADER_SYSTEM = (
    "You grade an AI agent's transcript against a rubric. "
    "Reply with PASS or FAIL on the first line, then one sentence why."
)
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class EvalSpec:
    """What one eval tests: its cases, and the text it imports from the source."""

    name: str
    directory: Path
    system: str
    tools: list[ToolSpec]


def run_sample(
    spec: EvalSpec, case: Case, client: ModelClient, sample: int
) -> dict[str, Any]:
    world = World.from_data(spec.tools, case.world)
    transcript: list[Event] = [UserMessage(case.prompt)]
    calls: list[ToolCall] = []
    cost = 0.0
    text = ""
    turns = 0
    finished = False
    while turns < case.max_turns:
        turn = client.complete(
            system=spec.system, tools=spec.tools, transcript=transcript
        )
        turns += 1
        cost += turn.cost_usd
        text = turn.text
        transcript.append(turn)
        if not turn.calls:
            finished = True
            break
        calls += turn.calls
        transcript.append([world.call(c) for c in turn.calls])
    failures = grade(case, calls, finished=finished)
    if case.checks.rubric:
        verdict = client.complete(
            system=GRADER_SYSTEM,
            tools=[],
            transcript=[UserMessage(_rubric_prompt(case, calls, text))],
        )
        cost += verdict.cost_usd
        if not verdict.text.strip().upper().startswith("PASS"):
            failures.append(f"rubric: {verdict.text.strip()[:200]}")
    return {
        "case": case.id,
        "sample": sample,
        "passed": not failures,
        "failures": failures,
        "calls": [{"name": c.name, "args": c.args} for c in calls],
        "final_text": text,
        "turns": turns,
        "cost_usd": cost,
    }


def _rubric_prompt(case: Case, calls: list[ToolCall], final_text: str) -> str:
    made = "\n".join(f"- {c.name} {json.dumps(c.args)}" for c in calls) or "- (none)"
    return (
        f"Task given to the agent:\n{case.prompt}\n\nRubric:\n{case.checks.rubric}\n\n"
        f"Tool calls the agent made:\n{made}\n\nIts final message:\n{final_text}"
    )


def run_eval(
    spec: EvalSpec,
    client: ModelClient,
    *,
    samples: int,
    only: list[str] | None = None,
) -> list[dict[str, Any]]:
    cases = load_cases(spec.directory / "cases")
    if only:
        unknown = set(only) - {c.id for c in cases}
        if unknown:
            raise ValueError(f"unknown case: {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id in only]
    return [run_sample(spec, case, client, n) for case in cases for n in range(samples)]


def summarize(
    spec: EvalSpec,
    rows: list[dict[str, Any]],
    *,
    model: str,
    samples: int,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = cases.setdefault(row["case"], {"samples": 0, "failed_samples": 0})
        entry["samples"] += 1
        entry["failed_samples"] += not row["passed"]
    for entry in cases.values():
        entry["failed"] = case_failed(entry["failed_samples"], entry["samples"])
    summary: dict[str, Any] = {
        "eval": spec.name,
        "model": model,
        "samples": samples,
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "cases": cases,
        "failed_cases": sorted(c for c, e in cases.items() if e["failed"]),
    }
    if baseline is not None:
        shared = cases.keys() & baseline["cases"].keys()
        summary["baseline"] = {
            "cost_usd": baseline["cost_usd"],
            "regressed": sorted(
                c
                for c in shared
                if cases[c]["failed"] and not baseline["cases"][c]["failed"]
            ),
            "fixed": sorted(
                c
                for c in shared
                if not cases[c]["failed"] and baseline["cases"][c]["failed"]
            ),
        }
    return summary


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"{summary['eval']} on {summary['model']}: "
        f"{len(summary['cases']) - len(summary['failed_cases'])}/"
        f"{len(summary['cases'])} cases passed "
        f"({summary['samples']} samples each), cost ${summary['cost_usd']:.4f}"
    ]
    lines += [f"  FAIL {c}" for c in summary["failed_cases"]]
    if "baseline" in summary:
        base = summary["baseline"]
        lines.append(
            f"  vs baseline (cost ${base['cost_usd']:.4f}): "
            f"regressed {base['regressed'] or 'none'}, fixed {base['fixed'] or 'none'}"
        )
    return "\n".join(lines)


def load_baseline(directory: Path, label: str, model: str) -> dict[str, Any]:
    path = directory / label / "summary.json"
    if not path.is_file():
        raise SystemExit(f"no baseline run at {path}")
    baseline = json.loads(path.read_text())
    if baseline["model"] != model:
        raise SystemExit(
            f"baseline ran {baseline['model']}, not {model}; compare the same model"
        )
    return baseline


def resolve_client(target: str) -> ClientFactory:
    """`package.module:factory`, where factory(model) returns a ModelClient."""
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def main(spec: EvalSpec, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"evals/{spec.name}/run.py")
    parser.add_argument("--out", required=True, help="run label; writes runs/<label>/")
    parser.add_argument("--model", required=True)
    parser.add_argument("--client", required=True, help="module:factory(model)")
    parser.add_argument("--baseline", help="label of an earlier run on the same model")
    parser.add_argument("--case", action="append", help="run only this case id")
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args(argv)
    runs = spec.directory / "runs"
    for label in (args.out, args.baseline or "x"):
        if not LABEL.match(label):
            parser.error(f"bad run label: {label}")
    baseline = load_baseline(runs, args.baseline, args.model) if args.baseline else None
    client = resolve_client(args.client)(args.model)
    rows = run_eval(spec, client, samples=args.samples, only=args.case)
    summary = summarize(
        spec, rows, model=args.model, samples=args.samples, baseline=baseline
    )
    out = runs / args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(format_summary(summary), file=sys.stdout)
    failed = summary["baseline"]["regressed"] if baseline else summary["failed_cases"]
    return 1 if failed else 0
