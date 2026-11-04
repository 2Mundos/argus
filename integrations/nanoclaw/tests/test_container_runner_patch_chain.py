"""Real install patch order: resource hygiene then git worker attribution."""

import subprocess
import sys
from pathlib import Path

HYGIENE = Path(__file__).parents[1] / "patch-agent-resource-hygiene.py"
GIT = Path(__file__).parents[1] / "patch-git-worker-attribution.py"
ANCHOR = "  if (securityProfile) agent.securityProfile = securityProfile;\n"
SHIM = "/usr/local/lib/argus/argus-oom-priority.sh"
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


def test_install_chain_produces_valid_compose_session_env(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    assert _run(HYGIENE, root).returncode == 0
    mid = (root / "src" / "container-runner.ts").read_text()
    assert "// harness's command shells" in mid
    assert f"BASH_ENV: '{SHIM}'" in mid
    assert mid.count("agent.env = {") == 1
    assert "// argus: git worker attribution" not in mid

    git_run = _run(GIT, root)
    assert git_run.returncode == 0, git_run.stderr
    text = (root / "src" / "container-runner.ts").read_text()
    assert text.count("agent.env = {") == 1
    assert f"PATH: '{BIN}:'" in text
    assert "...agent.env," in text
    assert text.index("...agent.env,") < text.index(f"PATH: '{BIN}:'")
    assert text.rfind("...agent.env,") == text.index("...agent.env,")
    assert f"BASH_ENV: '{SHIM}'" in text
    assert "// harness's command shells" in text
    assert "// argus: agent resource hygiene" in text
    assert text.index("// argus: agent resource hygiene") < text.index("PATH:")
    assert _run(GIT, root).stdout == ""


def test_git_patch_fails_without_hygiene_block(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    result = _run(GIT, root)
    assert result.returncode != 0
    assert "patch-agent-resource-hygiene" in result.stderr + result.stdout
