"""patch-merge-auth-env.py against excerpts of container-runner.ts in each patched state."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-merge-auth-env.py"
NAMES = [
    "ARGUS_MERGE_AUTH_KEY",
    "ARGUS_MERGE_AUTH_TASK",
    "ARGUS_MERGE_AUTH_ATTEMPT",
    "ARGUS_MERGE_AUTHORIZED",
]
TEMPLATE = """  const contributedEnv: Record<string, string> = {{
    ...(contribution.env ?? {{}}),
  }};
  for (const key of [{names}]) {{
    const value = process.env[key];
    if (value?.trim()) contributedEnv[key] = value;
  }}
"""
STATES = [
    "'ARGUS_GIT_AUTHOR_NAME', 'ARGUS_GIT_AUTHOR_EMAIL', 'ARGUS_COMMAND_GUARD'",
    "'ARGUS_GIT_AUTHOR_NAME', 'ARGUS_GIT_AUTHOR_EMAIL', 'ARGUS_COMMAND_GUARD', 'ARGUS_FORGE_REVIEW'",
]


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


def _forwarded(root: Path) -> list[str]:
    text = (root / "src" / "container-runner.ts").read_text()
    match = re.search(r"for \(const key of \[([^\]]*)\]\)", text)
    assert match
    return re.findall(r"'([A-Z_]+)'", match.group(1))


@pytest.mark.parametrize("state", STATES)
def test_extends_a_patched_runner_once_and_keeps_earlier_names(
    tmp_path: Path, state: str
) -> None:
    root = _checkout(tmp_path, TEMPLATE.format(names=state))
    before = re.findall(r"'([A-Z_]+)'", state)
    assert _run(root).returncode == 0
    assert _forwarded(root) == before + NAMES
    text = (root / "src" / "container-runner.ts").read_text()
    assert _run(root).returncode == 0
    assert (root / "src" / "container-runner.ts").read_text() == text


def test_fails_closed_without_the_forwarded_list(tmp_path: Path) -> None:
    body = "const contributedEnv = {};\n"
    root = _checkout(tmp_path, body)
    result = _run(root)
    assert result.returncode != 0
    assert "changed upstream" in result.stderr + result.stdout
    assert (root / "src" / "container-runner.ts").read_text() == body
