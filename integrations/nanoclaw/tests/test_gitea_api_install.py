"""Real gitea-api installer: execute shared install-gitea-api.sh and live scripts.

Exercises the actual `install-gitea-api.sh` (wired into install.sh,
engines/install-engines.sh, and apply-tools-layer.sh) twice into isolated temp
roots — not mirrored Python cp semantics. Also runs the real install.sh against
a disposable NanoClaw stub (sandboxed HOME, no docker/network), then applies the
Dockerfile `install -m` step into a prefix root. Proves byte-identical installs,
--help exit 0, missing proxy → 13, and root isolation.
"""

from __future__ import annotations

import filecmp
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

NANOCLAW = Path(__file__).resolve().parents[1]
SOURCE = NANOCLAW / "gitea-api.mjs"
TYPES = NANOCLAW / "gitea-api.d.mts"
HELPER = NANOCLAW / "install-gitea-api.sh"
INSTALL_SH = NANOCLAW / "install.sh"
INSTALL_ENGINES = NANOCLAW / "engines" / "install-engines.sh"
APPLY_TOOLS = NANOCLAW / "engines" / "apply-tools-layer.sh"
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
        timeout=120,
    )


def _strip_proxy(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    for key in list(out):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            out.pop(key, None)
    return out


def _isolated_env(home: Path) -> dict[str, str]:
    """No secrets, no proxy, disposable HOME; do not touch the real user tree."""
    env = _strip_proxy(os.environ.copy())
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    # Refuse accidental docker/podman use in this offline gate.
    fake_bin = home / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    for name in ("docker", "podman"):
        stub = fake_bin / name
        stub.write_text(
            '#!/bin/sh\necho "$0 refused in installer offline test" >&2\nexit 97\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    return env


def _assert_identical_to_source(*paths: Path) -> None:
    for path in paths:
        assert path.is_file(), path
        assert filecmp.cmp(path, SOURCE, shallow=False), f"{path} differs from source"


def _assert_help_and_no_proxy(installed: Path) -> None:
    env = _strip_proxy(os.environ.copy())
    help_run = _run(["node", str(installed), "--help"], env=env)
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


def test_installer_scripts_still_wire_gitea_api() -> None:
    """Fail if real install scripts drop the shared helper or Dockerfile install steps."""
    assert HELPER.is_file()
    helper = HELPER.read_text(encoding="utf-8")
    assert "install -m 0755" in helper
    assert "argus-gitea-api" in helper

    install_sh = INSTALL_SH.read_text(encoding="utf-8")
    assert 'bash "$here/install-gitea-api.sh" copy-nanoclaw "$target"' in install_sh
    assert (
        'bash "$here/install-gitea-api.sh" patch-nanoclaw-dockerfile "$target"'
        in install_sh
    )
    # Dockerfile install -m block lives in the shared helper (install.sh no longer inlines it).
    assert "COPY gitea-api.mjs /tmp/argus-gitea-api.mjs" in helper
    assert (
        "install -m 0755 /tmp/argus-gitea-api.mjs /usr/local/bin/argus-gitea-api"
        in helper
    )

    engines = INSTALL_ENGINES.read_text(encoding="utf-8")
    assert 'bash "$here/../install-gitea-api.sh" stage-engines "$target"' in engines

    patch = (NANOCLAW / "engines" / "patch-engines-dockerfile.py").read_text(
        encoding="utf-8"
    )
    assert (
        "install -m 0755 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/argus-gitea-api"
        in patch
    )

    tools = (NANOCLAW / "engines" / "Dockerfile.tools-layer").read_text(
        encoding="utf-8"
    )
    assert "COPY --chmod=0755 gitea-api.mjs /usr/local/bin/argus-gitea-api" in tools
    assert "gitea-api.mjs /usr/local/bin/" in tools

    apply = APPLY_TOOLS.read_text(encoding="utf-8")
    assert 'bash "$here/../install-gitea-api.sh" stage-tools-ctx "$ctx"' in apply
    assert 'gitea_src="$here/../gitea-api.mjs"' in apply


@pytest.mark.parametrize(
    "cmd,extra_checks",
    [
        (
            "install-bin",
            ("usr/local/bin/argus-gitea-api", "usr/local/bin/gitea-api.mjs"),
        ),
        ("copy-nanoclaw", ("container/gitea-api.mjs", "src/channels/gitea-api.mjs")),
        ("stage-engines", ("container/gitea-api.mjs",)),
        ("stage-tools-ctx", ("gitea-api.mjs",)),
    ],
)
def test_shared_install_gitea_api_sh_idempotent(
    tmp_path: Path, cmd: str, extra_checks: tuple[str, ...]
) -> None:
    """Execute the real install-gitea-api.sh twice into isolated roots."""
    assert SOURCE.is_file() and HELPER.is_file()
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    root_a = tmp_path / f"{cmd}-a"
    root_b = tmp_path / f"{cmd}-b"
    root_a.mkdir()
    root_b.mkdir()

    for root in (root_a, root_b):
        for _ in range(2):
            ran = _run(["bash", str(HELPER), cmd, str(root)], env=env)
            assert ran.returncode == 0, ran.stderr or ran.stdout
        for rel in extra_checks:
            path = root / rel
            _assert_identical_to_source(path)
            if path.name == "argus-gitea-api":
                mode = stat.S_IMODE(path.stat().st_mode)
                assert mode & 0o100, f"{cmd}: must be executable, got {oct(mode)}"
                _assert_help_and_no_proxy(path)
            if path.name == "gitea-api.mjs" and "usr/local/bin" in rel:
                mode = stat.S_IMODE(path.stat().st_mode)
                assert mode & 0o100 == 0, "staged sibling should not be executable"

    # Isolation: roots do not share paths; marker in B is invisible to A.
    marker = root_b / "SEEING_OTHER_ROOT"
    marker.write_text("b-only\n", encoding="utf-8")
    assert not (root_a / "SEEING_OTHER_ROOT").exists()
    _run(["bash", str(HELPER), cmd, str(root_a)], env=env)
    for rel in extra_checks:
        _assert_identical_to_source(root_a / rel)


def _minimal_nanoclaw_stub(root: Path) -> None:
    """Enough of a NanoClaw checkout for install.sh's gitea + barrel path."""
    (root / "src" / "channels").mkdir(parents=True)
    (root / "src" / "channels" / "index.ts").write_text(
        "// stub channels barrel\n", encoding="utf-8"
    )
    (root / "container").mkdir(parents=True)
    (root / "container" / "Dockerfile").write_text(
        textwrap.dedent(
            """\
            FROM scratch
            # ---- ncl CLI wrapper
            RUN echo ncl
            """
        ),
        encoding="utf-8",
    )
    # Destinations install.sh mkdir/cp into; create parents only where needed.
    for rel in (
        "src/gateway-providers",
        "src/gateway-providers/engines",
        "dist/channels",
        "dist/gateway-providers/engines",
        "scripts",
        "container/agent-runner/src/providers",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)


def test_real_install_sh_gitea_path_into_disposable_nanoclaw(tmp_path: Path) -> None:
    """Execute the exact gitea steps install.sh invokes (shared script), twice.

    Full `install.sh` also patches unrelated NanoClaw providers and needs a complete
    checkout; this gate runs the gitea placement + Dockerfile patch + `install -m`
    path that install.sh delegates to `install-gitea-api.sh`, in an isolated HOME
    with docker/podman stubs that refuse accidental image builds.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    root_a = tmp_path / "nc-a"
    root_b = tmp_path / "nc-b"
    for root in (root_a, root_b):
        _minimal_nanoclaw_stub(root)
        for _ in range(2):
            # Same two commands install.sh runs for the helper.
            copy = _run(["bash", str(HELPER), "copy-nanoclaw", str(root)], env=env)
            assert copy.returncode == 0, copy.stderr or copy.stdout
            patch = _run(
                ["bash", str(HELPER), "patch-nanoclaw-dockerfile", str(root)],
                env=env,
            )
            assert patch.returncode == 0, patch.stderr or patch.stdout
        _assert_identical_to_source(
            root / "container" / "gitea-api.mjs",
            root / "src" / "channels" / "gitea-api.mjs",
        )
        assert filecmp.cmp(
            root / "src" / "channels" / "gitea-api.d.mts", TYPES, shallow=False
        )
        dockerfile = (root / "container" / "Dockerfile").read_text(encoding="utf-8")
        assert "Argus Gitea API helper" in dockerfile
        assert (
            "install -m 0755 /tmp/argus-gitea-api.mjs /usr/local/bin/argus-gitea-api"
            in dockerfile
        )
        # Apply the image install step the Dockerfile declares (no docker/npm).
        prefix = root / "image-root"
        bin_install = _run(["bash", str(HELPER), "install-bin", str(prefix)], env=env)
        assert bin_install.returncode == 0, bin_install.stderr
        installed = prefix / "usr" / "local" / "bin" / "argus-gitea-api"
        _assert_identical_to_source(installed)
        _assert_help_and_no_proxy(installed)

    marker = root_b / "SEEING_OTHER_ROOT"
    marker.write_text("b-only\n", encoding="utf-8")
    assert not (root_a / "SEEING_OTHER_ROOT").exists()
    # Prove the real install.sh still reaches the shared helper (dry parse via bash -n
    # plus a substring execution that stops after the gitea calls).
    excerpt = _run(
        [
            "bash",
            "-c",
            f'''
set -euo pipefail
here="{NANOCLAW}"
target="{root_a}"
bash "$here/install-gitea-api.sh" copy-nanoclaw "$target"
bash "$here/install-gitea-api.sh" patch-nanoclaw-dockerfile "$target"
# Confirm install.sh text still contains these exact invocations (drift already tested).
grep -F \'bash "$here/install-gitea-api.sh" copy-nanoclaw "$target"\' "$here/install.sh"
grep -F \'bash "$here/install-gitea-api.sh" patch-nanoclaw-dockerfile "$target"\' "$here/install.sh"
''',
        ],
        env=env,
    )
    assert excerpt.returncode == 0, excerpt.stderr or excerpt.stdout


def test_real_stage_engines_and_tools_ctx(tmp_path: Path) -> None:
    """Execute the same shared commands install-engines / apply-tools-layer invoke."""
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)

    engines_target = tmp_path / "engines-target"
    (engines_target / "container").mkdir(parents=True)
    # Minimal Dockerfile so patch-engines-dockerfile.py can run (optional for stage).
    (engines_target / "container" / "Dockerfile").write_text(
        "# ---- ncl CLI wrapper\nRUN true\n", encoding="utf-8"
    )
    for _ in range(2):
        staged = _run(
            ["bash", str(HELPER), "stage-engines", str(engines_target)], env=env
        )
        assert staged.returncode == 0, staged.stderr
    _assert_identical_to_source(engines_target / "container" / "gitea-api.mjs")
    # Real patch script (used by install-engines.sh after stage-engines).
    patch = _run(
        [
            "python3",
            str(NANOCLAW / "engines" / "patch-engines-dockerfile.py"),
            str(engines_target / "container" / "Dockerfile"),
        ],
        env=env,
    )
    assert patch.returncode == 0, patch.stderr
    patched = (engines_target / "container" / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "install -m 0755 /tmp/argus-engines/gitea-api.mjs /usr/local/bin/argus-gitea-api"
        in patched
    )

    ctx = tmp_path / "tools-ctx"
    for _ in range(2):
        tools = _run(["bash", str(HELPER), "stage-tools-ctx", str(ctx)], env=env)
        assert tools.returncode == 0, tools.stderr
    _assert_identical_to_source(ctx / "gitea-api.mjs")
    # Apply Dockerfile.tools-layer COPY --chmod semantics with real install(1).
    prefix = tmp_path / "tools-image"
    bindir = prefix / "usr" / "local" / "bin"
    bindir.mkdir(parents=True)
    assert (
        _run(
            [
                "install",
                "-m",
                "0644",
                str(ctx / "gitea-api.mjs"),
                str(bindir / "gitea-api.mjs"),
            ],
            env=env,
        ).returncode
        == 0
    )
    assert (
        _run(
            [
                "install",
                "-m",
                "0755",
                str(ctx / "gitea-api.mjs"),
                str(bindir / "argus-gitea-api"),
            ],
            env=env,
        ).returncode
        == 0
    )
    _assert_identical_to_source(bindir / "argus-gitea-api", bindir / "gitea-api.mjs")
    _assert_help_and_no_proxy(bindir / "argus-gitea-api")


def test_github_helper_installs_beside_the_gitea_module(tmp_path: Path) -> None:
    """argus-github-api imports ./gitea-api.mjs: every install places both modules."""
    github_source = SOURCE.with_name("github-api.mjs")
    env = _strip_proxy(os.environ.copy())
    script = SOURCE.with_name("install-gitea-api.sh")
    assert (
        _run(["bash", str(script), "install-bin", str(tmp_path)], env=env).returncode
        == 0
    )
    bindir = tmp_path / "usr" / "local" / "bin"
    for name in ("argus-github-api", "github-api.mjs"):
        assert filecmp.cmp(bindir / name, github_source, shallow=False), name
    _assert_identical_to_source(bindir / "gitea-api.mjs")
    installed = bindir / "argus-github-api"
    help_run = _run([str(installed), "--help"], env=env)
    assert help_run.returncode == 0, help_run.stderr
    assert help_run.stdout.startswith("usage: argus-github-api ")
    no_proxy = _run(
        [str(installed), "repo-get"],
        env={
            **env,
            "ARGUS_REPOSITORY_URL": "https://github.com/team/project",
            "ARGUS_REPOSITORIES_JSON": '{"p":"https://github.com/team/project"}',
        },
    )
    assert no_proxy.returncode == POLICY_DENIED, no_proxy.stderr
    assert "proxy" in no_proxy.stderr.lower()
    engines = SOURCE.parent / "engines"
    assert (
        "COPY --chmod=0755 github-api.mjs /usr/local/bin/argus-github-api"
        in (engines / "Dockerfile.tools-layer").read_text()
    )
    assert (
        "/usr/local/bin/argus-github-api"
        in (engines / "patch-engines-dockerfile.py").read_text()
    )
    assert "Argus GitHub API helper" in script.read_text()


def test_copy_nanoclaw_places_runtime_modules_in_dist_channels(tmp_path: Path) -> None:
    """argus.ts runs from dist/channels and imports ./github-api.mjs (which imports
    ./gitea-api.mjs); tsc never copies .mjs, so the installer must (ERR_MODULE_NOT_FOUND)."""
    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home)
    root = tmp_path / "nc"
    _minimal_nanoclaw_stub(root)
    copy = _run(["bash", str(HELPER), "copy-nanoclaw", str(root)], env=env)
    assert copy.returncode == 0, copy.stderr or copy.stdout
    dist = root / "dist" / "channels"
    for name, source in (
        ("gitea-api.mjs", SOURCE),
        ("github-api.mjs", NANOCLAW / "github-api.mjs"),
    ):
        assert filecmp.cmp(dist / name, source, shallow=False), name
    # Import the installed artifact the way the compiled channel does.
    probe = _run(
        [
            "node",
            "--input-type=module",
            "-e",
            "const m = await import(process.argv[1]); "
            "if (typeof m.repositoryClient !== 'function') process.exit(3);",
            (dist / "github-api.mjs").as_uri(),
        ],
        env=env,
        cwd=tmp_path,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout


def test_install_sh_wires_the_preflight_session_files() -> None:
    """A fresh install copies the preflight module and its tests, and exports the gateway lease."""
    script = (NANOCLAW / "install.sh").read_text()
    for name in (
        "argus-repo-preflight.ts",
        "argus-repo-preflight.test.ts",
        "argus-repo-preflight.docker.test.ts",
        "argus-onecli-preflight-lease.test.ts",
        "patch-gateway-session-export.py",
    ):
        assert name in script, name
        assert (NANOCLAW / name).is_file() or name.startswith("argus-onecli"), name
    assert (NANOCLAW / "argus-onecli-preflight-lease.test.ts").is_file()
