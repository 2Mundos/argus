"""patch-gateway-session-export.py against an excerpt of NanoClaw's container-runner.ts
(pristine 8fbacc91, the function head verbatim)."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-gateway-session-export.py"
HEAD = "async function ensureGatewaySession(input: GatewaySessionInput): Promise<GatewaySessionControl> {\n"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _checkout(tmp_path: Path, body: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "container-runner.ts").write_text(body)
    return tmp_path


def test_exports_once_and_is_idempotent(tmp_path: Path) -> None:
    root = _checkout(tmp_path, f"// before\n{HEAD}  return x;\n}}\n")
    assert _run(root).returncode == 0
    text = (root / "src" / "container-runner.ts").read_text()
    assert text.count("export async function ensureGatewaySession(") == 1
    assert _run(root).returncode == 0
    assert (root / "src" / "container-runner.ts").read_text() == text


@pytest.mark.parametrize("body", ["// nothing here\n", HEAD + HEAD])
def test_fails_closed_when_the_anchor_is_missing_or_ambiguous(
    tmp_path: Path, body: str
) -> None:
    root = _checkout(tmp_path, body)
    result = _run(root)
    assert result.returncode != 0
    assert "upstream shape changed" in (result.stderr + result.stdout)
    assert (root / "src" / "container-runner.ts").read_text() == body


def test_fails_closed_when_the_file_is_missing(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode != 0
