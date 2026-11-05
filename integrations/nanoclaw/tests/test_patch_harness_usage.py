"""patch-harness-usage.py: the Codex live-log hooks, against excerpts of codex.ts
(the notification switch; `item/completed` present or absent)."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "patch-harness-usage.py"
REGISTER = "import { registerProvider } from './provider-registry.js';\n"

SWITCH = """async function* runOneTurn(
  x
) {
    switch (method) {
%s      case 'turn/completed': {
        a();
      }
    }
}
"""
ITEM = "      case 'item/completed': {\n        foo();\n        break;\n      }\n"


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def codex_ts(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "container/agent-runner/src/providers/codex.ts"
    p.parent.mkdir(parents=True)
    p.write_text(REGISTER + body)
    return p


def test_adds_log_cases_and_is_idempotent(tmp_path: Path) -> None:
    p = codex_ts(tmp_path, SWITCH % "")
    assert run(tmp_path).returncode == 0
    first = p.read_text()
    assert run(tmp_path).returncode == 0
    assert p.read_text() == first
    for method in ("item/completed", "error", "turn/completed"):
        assert first.count(f"recordHarnessCodexLog('{method}', params);") == 1
    assert first.count("recordHarnessUsage(\n") == 1


def test_joins_an_existing_item_completed_case(tmp_path: Path) -> None:
    p = codex_ts(tmp_path, SWITCH % ITEM)
    assert run(tmp_path).returncode == 0
    text = p.read_text()
    assert text.count("case 'item/completed'") == 1
    assert (
        "case 'item/completed': {\n        recordHarnessCodexLog('item/completed', params);\n        foo();"
        in text
    )


def test_stops_on_an_unrecognised_item_completed_shape(tmp_path: Path) -> None:
    body = SWITCH % "      case 'item/completed':\n        foo();\n        break;\n"
    codex_ts(tmp_path, body)
    res = run(tmp_path)
    assert res.returncode != 0
    assert "upstream shape changed" in (res.stderr + res.stdout)


def test_stops_when_the_anchor_is_gone(tmp_path: Path) -> None:
    codex_ts(tmp_path, "async function* runOneTurn(\n  x\n) {}\n")
    assert run(tmp_path).returncode != 0
