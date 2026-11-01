"""Does the orchestrator choose the right Argus tools? See README.md."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from argus_core.auth import Scope  # noqa: E402

from evals.runner import EvalSpec, main  # noqa: E402
from evals.source import orchestrator_prompt, tools_for  # noqa: E402


def build() -> EvalSpec:
    return EvalSpec(
        name="orchestrator-tools",
        directory=HERE,
        system=orchestrator_prompt(),
        tools=tools_for(Scope.ORCHESTRATOR),
    )


if __name__ == "__main__":
    raise SystemExit(main(build()))
