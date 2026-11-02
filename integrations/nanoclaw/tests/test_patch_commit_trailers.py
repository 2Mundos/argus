"""patch-commit-trailers.py against NanoClaw's core canon container/CLAUDE.md.

The fixture is the pristine upstream file from NanoClaw commit
8fbacc91b6e2671d53c17fd0c866c0e2eef21715 (container/CLAUDE.md), fetched from
https://raw.githubusercontent.com/qwibitai/nanoclaw/8fbacc91b6e2671d53c17fd0c866c0e2eef21715/container/CLAUDE.md
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "patch-commit-trailers.py"
FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "nanoclaw-canon" / "container-CLAUDE.md"
)
MARKER = "<!-- argus: no attribution trailers in commit messages -->"
PROHIBITION = "A commit message you create must not contain a `Co-Authored-By:` trailer"

# Harness providers compose from the single patched canon; executor-only paths do not.
HARNESS_COVERAGE = (
    ("claude", "CLAUDE.md", "canon"),
    ("codex", "AGENTS.md", "canon"),
    ("agent", None, "unsupported_by_canon"),
    ("opencode", None, "unsupported_by_canon"),
    ("copilot", None, "unsupported_by_canon"),
)


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _checkout(tmp_path: Path, body: str | None = None) -> Path:
    container = tmp_path / "container"
    container.mkdir()
    if body is not None:
        (container / "CLAUDE.md").write_text(body)
    return tmp_path


def test_applies_once_and_is_idempotent(tmp_path: Path) -> None:
    fixture = FIXTURE.read_text()
    root = _checkout(tmp_path, fixture)
    first = _run(root)
    assert first.returncode == 0, first.stderr
    assert f"patched {root / 'container' / 'CLAUDE.md'}" in first.stdout
    text = (root / "container" / "CLAUDE.md").read_text()
    assert text.count(MARKER) == 1
    assert text.count(PROHIBITION) == 1
    assert text.startswith(fixture)
    again = _run(root)
    assert again.returncode == 0, again.stderr
    assert again.stdout == ""
    assert (root / "container" / "CLAUDE.md").read_text() == text


def test_missing_canon_fails_closed(tmp_path: Path) -> None:
    root = _checkout(tmp_path, body=None)
    result = _run(root)
    assert result.returncode != 0
    err = result.stderr + result.stdout
    assert "CLAUDE.md" in err
    assert "missing" in err


def test_install_sh_runs_the_patch() -> None:
    script = (SCRIPT.parent / "install.sh").read_text()
    assert 'python3 "$here/patch-commit-trailers.py" "$target"' in script


def test_harness_paths_reach_the_rule_or_name_the_delegate_gap(tmp_path: Path) -> None:
    """Both harness providers share the patched canon; executor-only CLIs do not.

    claude → composed CLAUDE.md, codex → composed AGENTS.md, both from
    container/CLAUDE.md. Cursor/agent, opencode and copilot never load that
    project document; their coverage is the argus-delegate prompt prefix.
    """
    root = _checkout(tmp_path, FIXTURE.read_text())
    assert _run(root).returncode == 0
    canon = (root / "container" / "CLAUDE.md").read_text()
    assert MARKER in canon
    assert PROHIBITION in canon

    for provider, project_doc, coverage in HARNESS_COVERAGE:
        if coverage == "canon":
            assert project_doc in {"CLAUDE.md", "AGENTS.md"}
            # Single shared source: the installed canon carries the rule for both.
            assert MARKER in canon, (
                f"{provider} ({project_doc}) must see the patched canon"
            )
        else:
            assert coverage == "unsupported_by_canon"
            assert project_doc is None
            # Named limitation: no project document → delegate prefix only.
            assert provider in {"agent", "opencode", "copilot"}
