"""patch-gateway-sentinel.py against an excerpt of NanoClaw's drivers/types.ts (pristine 8fbacc91)."""

import subprocess
import sys
from pathlib import Path

import pytest

NANOCLAW = Path(__file__).parents[1]
SCRIPT = NANOCLAW / "patch-gateway-sentinel.py"
EXCERPT = """      if (looksLikeCredential(value)) {
        throw deniedByPolicy(`credential value in contributed env '${key}' on ${container.role}`);
      }
}

/**
 * Credential VALUES, recognised by issuer-assigned prefixes.
 */
export function looksLikeCredential(value: string): boolean {
  return /^github_pat_[A-Za-z0-9_]{20,}$/.test(value);
}
"""


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True, timeout=30
    )


def _checkout(tmp_path: Path, body: str) -> Path:
    (tmp_path / "src" / "drivers").mkdir(parents=True)
    (tmp_path / "src" / "drivers" / "types.ts").write_text(body)
    return tmp_path


def test_exempts_only_the_exact_sentinel_pair_and_is_idempotent(tmp_path: Path) -> None:
    root = _checkout(tmp_path, EXCERPT)
    assert _run(root).returncode == 0
    text = (root / "src/drivers/types.ts").read_text()
    stub = (NANOCLAW / "argus-copilot-stub.ts").read_text()
    sentinel = text.split("COPILOT_GITHUB_TOKEN: '")[1].split("'")[0]
    assert f'"{sentinel}"' in stub
    assert text.count("!isArgusGatewaySentinel(key, value) && looksLikeCredential(value)") == 1
    assert text.count("COPILOT_GITHUB_TOKEN:") == 1
    assert _run(root).returncode == 0
    assert (root / "src/drivers/types.ts").read_text() == text


@pytest.mark.parametrize("body", ["", EXCERPT.replace("looksLikeCredential(value)) {", "other(value)) {", 1)])
def test_refuses_a_changed_upstream_shape(tmp_path: Path, body: str) -> None:
    root = _checkout(tmp_path, body)
    result = _run(root)
    assert result.returncode != 0
    assert "changed upstream" in result.stderr
    assert (root / "src/drivers/types.ts").read_text() == body
