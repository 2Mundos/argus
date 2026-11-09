"""Import order: each module imports cleanly first in a fresh interpreter."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "argus_api.engine_choice",
        "argus_api.tasks.service",
        "argus_api.orchestrator",
        "argus_api.fleet.service",
        "argus_api.accounts.service",
    ],
)
def test_module_imports_first_in_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
