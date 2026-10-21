"""entrypoint.sh install-dir validation (security-hardening spec)."""

import subprocess
from pathlib import Path

SNIPPET = r"""
set -eu
INSTALL="$1"
case "$INSTALL" in
  /*) ;;
  *) echo "ARGUS_NANOCLAW_INSTALL_DIR must be an absolute path" >&2; exit 78 ;;
esac
case "$INSTALL" in
  *\'* | *\"* | *$'\n'* | *$'\r'*)
    echo "ARGUS_NANOCLAW_INSTALL_DIR must not contain quotes or newlines" >&2
    exit 78
    ;;
esac
node -e 'process.stdout.write(require(process.argv[1]).version)' "$INSTALL/package.json"
"""


def test_hostile_install_path_is_rejected(tmp_path: Path) -> None:
    hostile = str(tmp_path / "x'; echo pwned #")
    r = subprocess.run(
        ["sh", "-c", SNIPPET, "sh", hostile],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 78
    assert "quotes" in r.stderr or "absolute" in r.stderr


def test_version_read_via_argv(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_text('{"version":"1.2.3"}\n')
    r = subprocess.run(
        ["sh", "-c", SNIPPET, "sh", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "1.2.3"
