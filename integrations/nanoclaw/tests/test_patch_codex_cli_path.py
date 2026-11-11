"""patch-codex-cli-path.py against a codex-app-server.ts excerpt."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "patch-codex-cli-path.py"
EXCERPT = """import fs from 'fs';
import type { McpServerConfig } from './types.js';

export function buildCodexProcessEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const next: NodeJS.ProcessEnv = {};
  for (const key of CODEX_ENV_ALLOWLIST) {
    const value = env[key];
    if (value !== undefined) next[key] = value;
  }
  if (!next.HOME) next.HOME = '/home/node';
  return next;
}
"""


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _checkout(tmp_path: Path, text: str = EXCERPT) -> Path:
    providers = tmp_path / "container" / "agent-runner" / "src" / "providers"
    providers.mkdir(parents=True)
    (providers / "codex-app-server.ts").write_text(text)
    return providers / "codex-app-server.ts"


def test_wires_helper_after_allowlist_loop(tmp_path: Path) -> None:
    target = _checkout(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    text = target.read_text()
    assert "import { codexCliPath } from './codex-cli-path.js';" in text
    call = "const cliPath = codexCliPath(next.PATH, env.PNPM_HOME);"
    assert text.count(call) == 1
    assert "if (cliPath !== undefined) next.PATH = cliPath;" in text
    assert text.index("for (const key of CODEX_ENV_ALLOWLIST)") < text.index(call)
    assert text.index(call) < text.index("  return next;")


def test_idempotent(tmp_path: Path) -> None:
    target = _checkout(tmp_path)
    assert _run(tmp_path).returncode == 0
    once = target.read_text()
    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr
    assert target.read_text() == once


def test_fails_loudly_when_anchor_missing(tmp_path: Path) -> None:
    target = _checkout(tmp_path, EXCERPT.replace("buildCodexProcessEnv", "renamed"))
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "buildCodexProcessEnv not found" in result.stderr
    assert "codex-cli-path" not in target.read_text()


def test_ignores_old_host_tree_location(tmp_path: Path) -> None:
    providers = tmp_path / "src" / "providers"
    providers.mkdir(parents=True)
    (providers / "codex-app-server.ts").write_text(EXCERPT)
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "missing" in result.stderr


def test_fails_loudly_when_import_anchor_missing(tmp_path: Path) -> None:
    _checkout(
        tmp_path,
        EXCERPT.replace("import type { McpServerConfig }", "import type { X }"),
    )
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "import anchor not found" in result.stderr
