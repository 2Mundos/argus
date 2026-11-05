"""patch-git-worker-attribution.py after patch-agent-resource-hygiene.py (install order)."""

import json
import re
import shutil
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


def _eval_path(root: Path, agent_env: dict, env: dict) -> list[str]:
    text = (root / "src" / "container-runner.ts").read_text()
    match = re.search(r"^    PATH: (.*),$", text, re.MULTILINE)
    assert match
    script = (
        f"const agent = {{ env: {json.dumps(agent_env)} }};"
        f"console.log({match.group(1)});"
    )
    out = subprocess.run(
        [shutil.which("node") or "node", "-e", script],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=30,
    )
    return out.stdout.strip().split(":")


@pytest.mark.parametrize(
    ("agent_env", "env", "expected"),
    [
        ({}, {"PATH": "/usr/bin:/bin"}, [BIN, "/pnpm", "/usr/bin", "/bin"]),
        (
            {},
            {"PATH": "/usr/bin", "PNPM_HOME": "/opt/engines"},
            [BIN, "/opt/engines", "/usr/bin"],
        ),
        ({}, {"PATH": "/usr/bin:/pnpm:/bin"}, [BIN, "/usr/bin", "/pnpm", "/bin"]),
        (
            {"PATH": "/custom:/bin"},
            {"PATH": "/usr/bin"},
            [BIN, "/pnpm", "/custom", "/bin"],
        ),
    ],
    ids=["default", "pnpm-home", "already-present", "agent-path"],
)
def test_composed_path_order(
    post_hygiene: Path,
    agent_env: dict[str, str],
    env: dict[str, str],
    expected: list[str],
) -> None:
    assert _run(SCRIPT, post_hygiene).returncode == 0
    assert _eval_path(post_hygiene, agent_env, env) == expected


def test_rewrites_previous_path_shape(post_hygiene: Path) -> None:
    assert _run(SCRIPT, post_hygiene).returncode == 0
    target = post_hygiene / "src" / "container-runner.ts"
    new = re.search(r"^    PATH: .*,$", target.read_text(), re.MULTILINE)
    assert new
    old = (
        f"    PATH: '{BIN}:' + (agent.env.PATH ?? process.env.PATH ?? "
        "'/usr/local/bin:/usr/bin:/bin'),"
    )
    target.write_text(target.read_text().replace(new.group(0), old))
    assert _run(SCRIPT, post_hygiene).returncode == 0
    assert new.group(0) in target.read_text()
    assert _run(SCRIPT, post_hygiene).stdout == ""
