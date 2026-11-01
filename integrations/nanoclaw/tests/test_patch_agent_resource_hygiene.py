"""patch-agent-resource-hygiene.py against an excerpt of container-runner.ts.

The anchor is the `agent.securityProfile` hook that patch-codex-sandbox.py adds, so
the excerpt is that hook's span (composeSessionSpec). Running the real chain
(install.sh) needs a NanoClaw checkout; this pins the shape the patch relies on.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-agent-resource-hygiene.py"
ANCHOR = "  if (securityProfile) agent.securityProfile = securityProfile;\n"
SHIM = "/usr/local/lib/argus/argus-oom-priority.sh"
EXCERPT = f"""  const securityProfile = securityProfileForProvider(provider, process.env);
{ANCHOR}
  // The folder label (D9) rides the spec
"""


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


def test_applies_once_and_is_idempotent(tmp_path: Path) -> None:
    root = _checkout(tmp_path, EXCERPT)
    assert _run(root).returncode == 0
    text = (root / "src" / "container-runner.ts").read_text()
    assert text.count("agent.env = {") == 1
    assert f"BASH_ENV: '{SHIM}', CLAUDE_ENV_FILE: '{SHIM}', ...agent.env" in text
    assert text.index(ANCHOR) < text.index("agent.env = {")
    assert text.index("agent.env = {") < text.index("The folder label")
    assert _run(root).returncode == 0
    assert (root / "src" / "container-runner.ts").read_text() == text


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("// nothing here\n", id="missing"),
        pytest.param(EXCERPT + ANCHOR, id="duplicate-anchor"),
    ],
)
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
