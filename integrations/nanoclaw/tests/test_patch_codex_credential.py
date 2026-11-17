"""patch-codex-credential.py against a codex-app-server.ts excerpt."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "patch-codex-credential.py"
EXCERPT = """import type { McpServerConfig } from './types.js';

export function spawnCodexAppServer(): AppServer {
  const args = [...CODEX_APP_SERVER_ARGS];

  const proc = spawn('codex', args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: buildCodexProcessEnv(process.env),
  });
  return proc;
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


def test_checks_credential_on_the_env_the_child_gets(tmp_path: Path) -> None:
    target = _checkout(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    text = target.read_text()
    assert "import { requireCodexCredential } from './codex-credential.js';" in text
    assert text.count("const childEnv = buildCodexProcessEnv(process.env);") == 1
    assert "    env: childEnv," in text
    assert text.index("requireCodexCredential(childEnv);") < text.index("spawn('codex'")


def test_idempotent(tmp_path: Path) -> None:
    target = _checkout(tmp_path)
    assert _run(tmp_path).returncode == 0
    once = target.read_text()
    assert _run(tmp_path).returncode == 0
    assert target.read_text() == once


def test_fails_on_unknown_upstream_shape(tmp_path: Path) -> None:
    _checkout(tmp_path, EXCERPT.replace("spawn('codex'", "spawn('other'"))
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "upstream shape changed" in result.stderr
