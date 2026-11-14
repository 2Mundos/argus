"""patch-codex-hook-trust.py against codex-app-server.ts and its test, as excerpts."""

import subprocess
import sys
from pathlib import Path

import pytest

NANOCLAW = Path(__file__).parents[1]
SCRIPT = NANOCLAW / "patch-codex-hook-trust.py"
CLI_PATH_SCRIPT = NANOCLAW / "patch-codex-cli-path.py"
FORBIDDEN = ("--dangerously-bypass-hook-trust", "bypass_hook_trust")
SANDBOX = "const CODEX_SANDBOX_MODE = 'danger-full-access';"
APPROVAL = "const CODEX_APPROVAL_POLICY = 'never';"
EXCERPT = """import fs from 'fs';
import type { McpServerConfig } from './types.js';

export const CODEX_APP_SERVER_ARGS = ['--dangerously-bypass-hook-trust', 'app-server', '--listen', 'stdio://'] as const;

const CODEX_SANDBOX_MODE = 'danger-full-access';
const CODEX_APPROVAL_POLICY = 'never';

export function spawnCodexAppServer(): AppServer {
  // NanoClaw generates the hook config and has no interactive user available
  // to approve hook trust inside the container.
  const args = [...CODEX_APP_SERVER_ARGS];
}

export async function initializeCodexAppServer(server: AppServer): Promise<void> {
  if (resp.error) throw new Error(`initialize failed: ${resp.error.message}`);
  sendCodexNotification(server, 'initialized');
}

export async function startOrResumeCodexThread(): Promise<string> {
  const commonParams = {
    approvalPolicy: CODEX_APPROVAL_POLICY,
    sandbox: CODEX_SANDBOX_MODE,
    // App-server ignores the CLI hook-trust flag; apply it to every thread.
    config: { bypass_hook_trust: true },
    baseInstructions: params.baseInstructions,
  };
}

export function reconcileCodexHooksJson(): string {
  hooks.SessionStart = nextSessionStart;
  return JSON.stringify(hooksConfig, null, 2) + '\\n';
}

export function buildCodexProcessEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const next: NodeJS.ProcessEnv = {};
  return next;
}
"""
TEST_EXCERPT = """    expect(CODEX_APP_SERVER_ARGS).toContain('--dangerously-bypass-hook-trust');
    expect(requests[0].params.config).toEqual({ bypass_hook_trust: true });
    expect(requests[0].params.config).toEqual({ bypass_hook_trust: true });
"""


def _run(root: Path, script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), str(root)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _checkout(
    tmp_path: Path, text: str = EXCERPT, test: str | None = TEST_EXCERPT
) -> Path:
    providers = tmp_path / "container" / "agent-runner" / "src" / "providers"
    providers.mkdir(parents=True)
    (providers / "codex-app-server.ts").write_text(text)
    if test is not None:
        (providers / "codex-app-server.test.ts").write_text(test)
    return providers


def _assert_patched(providers: Path) -> None:
    text = (providers / "codex-app-server.ts").read_text()
    test = (providers / "codex-app-server.test.ts").read_text()
    for forbidden in FORBIDDEN:
        assert forbidden not in text
        assert forbidden not in test
    assert text.count("from './codex-hook-trust.js';") == 1
    assert (
        "CODEX_APP_SERVER_ARGS = ['app-server', '--listen', 'stdio://'] as const;"
        in text
    )
    assert SANDBOX in text and APPROVAL in text
    trust = text.index("await establishCodexHookTrust(")
    assert text.index("sendCodexNotification(server, 'initialized');") < trust
    assert trust < text.index("export async function startOrResumeCodexThread(")
    assert text.count("  withCodexGuardHook(hooksConfig);\n") == 1
    assert "registerExpectedCodexHook({ eventName: 'sessionStart'" in text
    assert test.count("expect(requests[0].params.config).toBeUndefined();") == 2


def test_patches_pristine_provider(tmp_path: Path) -> None:
    providers = _checkout(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    _assert_patched(providers)


def test_patches_provider_installed_by_cli_path(tmp_path: Path) -> None:
    providers = _checkout(tmp_path)
    assert _run(tmp_path, CLI_PATH_SCRIPT).returncode == 0
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    _assert_patched(providers)
    text = (providers / "codex-app-server.ts").read_text()
    assert "import { codexCliPath }" in text
    # A later cli-path run (an older install upgraded in place) keeps both.
    assert _run(tmp_path, CLI_PATH_SCRIPT).returncode == 0
    assert _run(tmp_path).returncode == 0
    assert (providers / "codex-app-server.ts").read_text() == text


def test_idempotent(tmp_path: Path) -> None:
    providers = _checkout(tmp_path)
    assert _run(tmp_path).returncode == 0
    once = [
        (providers / n).read_bytes()
        for n in sorted(p.name for p in providers.iterdir())
    ]
    second = _run(tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""
    again = [
        (providers / n).read_bytes()
        for n in sorted(p.name for p in providers.iterdir())
    ]
    assert again == once
    # Retained, not proof: the permissive upstream sandbox and approval constants pass
    # through unchanged; containment is the container profile (codex-hook-trust spec).
    text = (providers / "codex-app-server.ts").read_text()
    assert text.count(SANDBOX) == 1 and text.count(APPROVAL) == 1
    assert "sandbox: CODEX_SANDBOX_MODE" in text
    assert "approvalPolicy: CODEX_APPROVAL_POLICY" in text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            EXCERPT.replace(
                "['--dangerously-bypass-hook-trust', 'app-server',",
                "['app-server', '--dangerously-bypass-hook-trust',",
            ),
            "CODEX_APP_SERVER_ARGS not found",
        ),
        (
            EXCERPT.replace("apply it to every thread.", "for every thread."),
            "bypass_hook_trust still present",
        ),
        (
            EXCERPT.replace("'initialized'", "'ready'"),
            "initialize trust call anchor not found",
        ),
        (
            EXCERPT.replace("hooks.SessionStart = nextSessionStart;", "hooks.X = y;"),
            "reconcileCodexHooksJson return not found",
        ),
        (
            EXCERPT.replace("import type { McpServerConfig }", "import type { X }"),
            "codex-hook-trust import anchor not found",
        ),
    ],
    ids=["args", "thread-config", "initialize", "reconcile", "import"],
)
def test_changed_upstream_shape_fails_closed(
    tmp_path: Path, text: str, message: str
) -> None:
    providers = _checkout(tmp_path, text)
    result = _run(tmp_path)
    assert result.returncode != 0
    assert message in result.stderr
    assert "upstream shape changed" in result.stderr
    assert (providers / "codex-app-server.ts").read_text() == text
    assert (providers / "codex-app-server.test.ts").read_text() == TEST_EXCERPT


def test_changed_test_shape_fails_without_writing(tmp_path: Path) -> None:
    test = TEST_EXCERPT.replace("toContain(", "toContainEqual(")
    providers = _checkout(tmp_path, test=test)
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "upstream shape changed" in result.stderr
    assert (providers / "codex-app-server.ts").read_text() == EXCERPT


def test_missing_test_file_fails(tmp_path: Path) -> None:
    providers = _checkout(tmp_path, test=None)
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "codex-app-server.test.ts: missing" in result.stderr
    assert (providers / "codex-app-server.ts").read_text() == EXCERPT


def test_ignores_old_host_tree_location(tmp_path: Path) -> None:
    providers = tmp_path / "src" / "providers"
    providers.mkdir(parents=True)
    (providers / "codex-app-server.ts").write_text(EXCERPT)
    (providers / "codex-app-server.test.ts").write_text(TEST_EXCERPT)
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "missing" in result.stderr
    assert (providers / "codex-app-server.ts").read_text() == EXCERPT
