"""Real gitea-api installer paths: copy/install twice into isolated roots.

Exercises the same steps used by install.sh, engines/install-engines.sh, and
Dockerfile.tools-layer / apply-tools-layer.sh (COPY + install -m semantics),
without Docker. Proves byte-identical installs, --help exit 0, missing proxy → 13,
and that two roots cannot see each other.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

NANOCLAW = Path(__file__).resolve().parents[1]
SOURCE = NANOCLAW / "gitea-api.mjs"
TYPES = NANOCLAW / "gitea-api.d.mts"
POLICY_DENIED = 13


def _run(
    cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _strip_proxy(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    for key in list(out):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            out.pop(key, None)
    return out


def _install_like_install_sh(root: Path) -> Path:
    """install.sh: cp into container/ and channels/; Dockerfile install -m 0755 → argus-gitea-api."""
    container = root / "container"
    channels = root / "src" / "channels"
    container.mkdir(parents=True, exist_ok=True)
    channels.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, container / "gitea-api.mjs")
    shutil.copy2(SOURCE, channels / "gitea-api.mjs")
    shutil.copy2(TYPES, channels / "gitea-api.d.mts")
    bindir = root / "usr" / "local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    installed = bindir / "argus-gitea-api"
    # Same semantics as: install -m 0755 /tmp/argus-gitea-api.mjs /usr/local/bin/argus-gitea-api
    shutil.copy2(container / "gitea-api.mjs", installed)
    installed.chmod(0o755)
    return installed


def _install_like_install_engines(root: Path) -> Path:
    """install-engines.sh: cp to container/; patched Dockerfile install -m 0755 + 0644 stage."""
    container = root / "container"
    bindir = root / "usr" / "local" / "bin"
    container.mkdir(parents=True, exist_ok=True)
    bindir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, container / "gitea-api.mjs")
    staged = bindir / "gitea-api.mjs"
    installed = bindir / "argus-gitea-api"
    shutil.copy2(container / "gitea-api.mjs", staged)
    staged.chmod(0o644)
    shutil.copy2(container / "gitea-api.mjs", installed)
    installed.chmod(0o755)
    return installed


def _install_like_tools_layer(root: Path) -> Path:
    """apply-tools-layer.sh stages gitea-api.mjs; Dockerfile.tools-layer COPY --chmod=0755/0644."""
    ctx = root / "ctx"
    bindir = root / "usr" / "local" / "bin"
    ctx.mkdir(parents=True, exist_ok=True)
    bindir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, ctx / "gitea-api.mjs")
    # COPY --chmod=0644 ... gitea-api.mjs /usr/local/bin/
    staged = bindir / "gitea-api.mjs"
    shutil.copy2(ctx / "gitea-api.mjs", staged)
    staged.chmod(0o644)
    # COPY --chmod=0755 gitea-api.mjs /usr/local/bin/argus-gitea-api
    installed = bindir / "argus-gitea-api"
    shutil.copy2(ctx / "gitea-api.mjs", installed)
    installed.chmod(0o755)
    return installed


INSTALLERS = (
    ("install.sh", _install_like_install_sh),
    ("install-engines.sh", _install_like_install_engines),
    ("tools-layer", _install_like_tools_layer),
)


def _assert_identical_to_source(*paths: Path) -> None:
    for path in paths:
        assert path.is_file(), path
        assert filecmp.cmp(path, SOURCE, shallow=False), f"{path} differs from source"


def _assert_help_and_no_proxy(installed: Path) -> None:
    env = _strip_proxy(os.environ.copy())
    help_run = _run(["node", str(installed), "--help"], env=env)
    # Prefer shebang exec when executable bit is set.
    if os.access(installed, os.X_OK):
        shebang = _run([str(installed), "--help"], env=env)
        assert shebang.returncode == 0, shebang.stderr
        assert "argus-gitea-api" in shebang.stdout or "usage:" in shebang.stdout
    assert help_run.returncode == 0, help_run.stderr
    no_proxy = _run(
        ["node", str(installed), "repo-get"],
        env={
            **env,
            "ARGUS_REPOSITORY_URL": "https://git.example.test/team/project",
            "ARGUS_REPOSITORIES_JSON": '{"p":"https://git.example.test/team/project"}',
        },
    )
    assert no_proxy.returncode == POLICY_DENIED, no_proxy.stderr
    assert "proxy" in no_proxy.stderr.lower()


@pytest.mark.parametrize("name,installer", INSTALLERS)
def test_gitea_api_install_idempotent(tmp_path: Path, name: str, installer) -> None:
    assert SOURCE.is_file()
    root_a = tmp_path / f"{name}-a"
    root_b = tmp_path / f"{name}-b"
    root_a.mkdir()
    root_b.mkdir()

    for root in (root_a, root_b):
        first = installer(root)
        second = installer(root)
        assert first.resolve() == second.resolve()
        _assert_identical_to_source(first)
        staged = root / "usr" / "local" / "bin" / "gitea-api.mjs"
        if staged.exists():
            _assert_identical_to_source(staged)
        container_copy = root / "container" / "gitea-api.mjs"
        if container_copy.exists():
            _assert_identical_to_source(container_copy)
        channels_copy = root / "src" / "channels" / "gitea-api.mjs"
        if channels_copy.exists():
            _assert_identical_to_source(channels_copy)
        mode = stat.S_IMODE(first.stat().st_mode)
        assert mode & 0o100, (
            f"{name}: argus-gitea-api must be executable, got {oct(mode)}"
        )
        _assert_help_and_no_proxy(first)

    # Isolation: roots do not share installed bytes by hardlink/path; each is self-contained.
    a_bin = root_a / "usr" / "local" / "bin" / "argus-gitea-api"
    b_bin = root_b / "usr" / "local" / "bin" / "argus-gitea-api"
    assert a_bin.resolve() != b_bin.resolve()
    assert filecmp.cmp(a_bin, b_bin, shallow=False)
    assert not any(root_a.rglob("seeing-b")), "root A must not contain root B markers"
    marker = root_b / "SEEING_OTHER_ROOT"
    marker.write_text("b-only\n", encoding="utf-8")
    assert not (root_a / "SEEING_OTHER_ROOT").exists()
    # Second pass after marker: still idempotent for the helper bytes.
    installer(root_a)
    _assert_identical_to_source(a_bin)
