"""patch-git-worker-attribution.py after patch-agent-resource-hygiene.py (install order)."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-git-worker-attribution.py"
HYGIENE_SCRIPT = Path(__file__).parents[1] / "patch-agent-resource-hygiene.py"
ANCHOR = "  if (securityProfile) agent.securityProfile = securityProfile;\n"
BIN = "/usr/local/lib/argus/bin"
EXCERPT = f"""  const securityProfile = securityProfileForProvider(provider, process.env);
{ANCHOR}
  // The folder label (D9) rides the spec
"""


def _run(script: Path, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), str(root)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _checkout(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "container-runner.ts").write_text(EXCERPT)
    return tmp_path


@pytest.fixture
def post_hygiene(tmp_path: Path) -> Path:
    root = _checkout(tmp_path)
    assert _run(HYGIENE_SCRIPT, root).returncode == 0
    return root


def test_merges_path_into_hygiene_agent_env(post_hygiene: Path) -> None:
    first = _run(SCRIPT, post_hygiene)
    assert first.returncode == 0, first.stderr
    text = (post_hygiene / "src" / "container-runner.ts").read_text()
    assert f"PATH: '{BIN}:'" in text
    assert "...agent.env," in text
    assert text.index("...agent.env,") < text.index(f"PATH: '{BIN}:'")
    assert "// harness's command shells" in text
    again = _run(SCRIPT, post_hygiene)
    assert again.returncode == 0
    assert again.stdout == ""


def test_requires_hygiene_agent_env_block(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    result = _run(SCRIPT, root)
    assert result.returncode != 0
    assert "patch-agent-resource-hygiene" in result.stderr + result.stdout
