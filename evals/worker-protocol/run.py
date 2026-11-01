"""Does a worker follow the claim-to-finish protocol? See README.md."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from argus_core.auth import Scope  # noqa: E402

from evals.runner import EvalSpec, main  # noqa: E402
from evals.source import tools_for, worker_prompt  # noqa: E402


def build() -> EvalSpec:
    return EvalSpec(
        name="worker-protocol",
        directory=HERE,
        system=worker_prompt(),
        tools=tools_for(Scope.WORKER),
    )


if __name__ == "__main__":
    raise SystemExit(main(build()))
