"""patch-claude-rate-limit.py against excerpts of NanoClaw's agent runner (the hooked
spans copied verbatim). The patched runner's own suite (overlays/claude*.test.ts) runs
in the NanoClaw checkout after install.sh."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-claude-rate-limit.py"

CLAUDE_TS = r"""import { registerProvider } from './provider-registry.js';
import type { AgentProvider, AgentQuery, ProviderEvent, ProviderOptions, QueryInput } from './types.js';

    async function* translateEvents(): AsyncGenerator<ProviderEvent> {
      let messageCount = 0;
      let lastStreamActivityAt = 0;
      for await (const message of sdkResult) {
        if (aborted) return;
        if (message.type === 'system' && message.subtype === 'init') {
          yield { type: 'init', continuation: message.session_id };
        } else if (message.type === 'assistant') {
          const content = message.content;
        } else if (message.type === 'result') {
          const hint = SDK_NOTICES.get(candidate);
          const resultAsError = hint ? `${candidate}\n${hint}` : '';
          yield {
            type: 'result',
            text: resultAsError ? null : (m.result ?? null),
            isError,
            error: m.errors?.length ? m.errors.join('\n') : resultAsError || undefined,
          };
        }
      }
    }
"""

FORMATTER_TS = """export const PROVIDER_FAILURE_FIELD = 'providerFailure';

export type ProviderFailureKind = 'auth_expired';

export interface ProviderFailure {
  provider: string;
  kind: ProviderFailureKind;
}
"""

POLL_LOOP_TS = """import { stripHarnessTagArtifacts } from './harness-tag-strip.js';
import { isUploadTraceCommand, uploadTrace } from './upload-trace.js';

function classifyProviderFailure(providerName: string, error: string | undefined): ProviderFailure | undefined {
  if (providerName !== 'codex' || !error) return undefined;
  return undefined;
}
"""


def runner(root: Path, formatter: str = FORMATTER_TS, claude: str = CLAUDE_TS) -> Path:
    src = root / "container" / "agent-runner" / "src"
    (src / "providers").mkdir(parents=True)
    (src / "providers" / "claude.ts").write_text(claude)
    (src / "formatter.ts").write_text(formatter)
    (src / "poll-loop.ts").write_text(POLL_LOOP_TS)
    return src


def patch(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_hooks_claude_and_the_poll_loop_once(tmp_path: Path) -> None:
    src = runner(tmp_path)
    first = patch(tmp_path)
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("patched") == 3
    claude = (src / "providers" / "claude.ts").read_text()
    assert "import { claudeRateLimitError } from './claude-rate-limit.js';" in claude
    assert "let rateLimited = false;" in claude
    assert ".error === 'rate_limit') rateLimited = true;" in claude
    assert "claudeRateLimitError(m.result, rateLimited)" in claude
    assert "error: rateLimit ?? (m.errors?.length" in claude
    assert "'auth_expired' | 'rate_limit'" in (src / "formatter.ts").read_text()
    poll = (src / "poll-loop.ts").read_text()
    assert (
        "if (providerName === 'claude') return claudeRateLimitFailure(error);" in poll
    )

    before = {p: p.read_text() for p in src.rglob("*.ts")}
    again = patch(tmp_path)
    assert (again.returncode, again.stdout) == (0, "")
    assert {p: p.read_text() for p in src.rglob("*.ts")} == before


def test_without_the_provider_failure_field_only_claude_is_hooked(
    tmp_path: Path,
) -> None:
    formatter = "export const FAILURE_NOTICE_FIELD = 'failureNotice';\n"
    src = runner(tmp_path, formatter=formatter)
    result = patch(tmp_path)
    assert result.returncode == 0
    assert "notice text only" in result.stderr
    assert "claudeRateLimitError" in (src / "providers" / "claude.ts").read_text()
    assert (src / "formatter.ts").read_text() == formatter
    assert (src / "poll-loop.ts").read_text() == POLL_LOOP_TS


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("let lastStreamActivityAt = 0;", "let lastStreamAt = 0;"),
        ("text: resultAsError ? null", "text: resultAsError ? undefined"),
        (
            "        } else if (message.type === 'assistant') {\n",
            "        } else if (message.type === 'assistant') {\n"
            "        } else if (message.type === 'assistant') {\n",
        ),
    ],
)
def test_upstream_drift_stops_the_install(tmp_path: Path, old: str, new: str) -> None:
    claude = CLAUDE_TS.replace(old, new, 1)
    src = runner(tmp_path, claude=claude)
    result = patch(tmp_path)
    assert result.returncode != 0
    assert "upstream shape changed" in result.stderr
    assert (src / "providers" / "claude.ts").read_text() == claude
