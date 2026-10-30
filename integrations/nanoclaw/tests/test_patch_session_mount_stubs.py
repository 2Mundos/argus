"""patch-session-mount-stubs.py against an excerpt of NanoClaw's container-runner.ts
(pristine 8fbacc91; import tail and sessDir line verbatim)."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-session-mount-stubs.py"
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "session-mount-stubs"
    / "container-runner.excerpt.txt"
)
SESSDIR = "  const sessDir = sessionDir(agentGroup.id, session.id);\n"
IMPORT_TAIL = "} from './session-manager.js';\n"


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


def test_applies_once_and_is_idempotent(tmp_path: Path) -> None:
    body = FIXTURE.read_text()
    root = _checkout(tmp_path, body)
    assert _run(root).returncode == 0
    text = (root / "src" / "container-runner.ts").read_text()
    assert text.count("import { ensureSessionMountStubs }") == 1
    assert text.count("ensureSessionMountStubs(sessDir);") == 1
    assert text.index(SESSDIR) < text.index("ensureSessionMountStubs(sessDir);")
    assert _run(root).returncode == 0
    assert (root / "src" / "container-runner.ts").read_text() == text


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("// nothing here\n", id="missing"),
        pytest.param(FIXTURE.read_text() + SESSDIR, id="duplicate-sessDir"),
        pytest.param(
            FIXTURE.read_text().replace(IMPORT_TAIL, IMPORT_TAIL + IMPORT_TAIL, 1),
            id="duplicate-import",
        ),
    ],
)
def test_fails_closed_when_the_anchor_is_missing_or_ambiguous(
    tmp_path: Path, body: str
) -> None:
    root = _checkout(tmp_path, body)
    result = _run(root)
    assert result.returncode != 0
    assert "upstream shape changed" in (result.stderr + result.stdout)
    assert (root / "src" / "container-runner.ts").read_text() == body


def test_fails_closed_when_the_file_is_missing(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode != 0


def test_install_sh_copies_the_overlay_and_runs_the_patch() -> None:
    """A fresh install ships the overlay and its test, then patches the runner."""
    root = SCRIPT.parent
    script = (root / "install.sh").read_text()
    assert 'cp "$here/overlays/session-mount-stubs.ts"' in script
    assert "$here/overlays/session-mount-stubs.test.ts" in script
    assert 'python3 "$here/patch-session-mount-stubs.py" "$target"' in script
    # The copy lands next to container-runner.ts, which imports './session-mount-stubs.js'.
    assert '"$target/src/"' in script
    assert (root / "overlays" / "session-mount-stubs.ts").is_file()
    assert (root / "overlays" / "session-mount-stubs.test.ts").is_file()


def test_provision_check_runs_the_overlay_test() -> None:
    script = (SCRIPT.parent / "provision.sh").read_text()
    assert "src/session-mount-stubs.test.ts" in script


def test_fixture_anchors_are_the_patch_anchors() -> None:
    """The excerpt must keep carrying both anchors verbatim, or the patch is untested."""
    body = FIXTURE.read_text()
    assert body.count(IMPORT_TAIL) == 1
    assert body.count(SESSDIR) == 1
