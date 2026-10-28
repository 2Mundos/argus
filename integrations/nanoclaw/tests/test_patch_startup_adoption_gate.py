"""patch-startup-adoption-gate.py against an excerpt of NanoClaw's container-runner.ts
(pristine 8fbacc91, the function head verbatim). When ARGUS_NANOCLAW_INSTALL_DIR points at a
built NanoClaw checkout, also typechecks container-runner.ts after install."""

import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-startup-adoption-gate.py"
HEAD = "export async function adoptRunningSessions(): Promise<{ adopted: number; stopped: number }> {\n"
PINNED = "8fbacc91b6e2671d53c17fd0c866c0e2eef21715"
LIST_FAIL_SOFT = """  } catch (err) {
    log.warn('Failed to list existing sessions for adoption', { err });
    return { adopted: 0, stopped: 0 };
  }
"""
LIST_FAIL_CLOSED = """  } catch (err) {
    log.warn('Failed to list existing sessions for adoption', { err });
    throw err;
  }
"""
NANOCLAW_INSTALL = os.environ.get("ARGUS_NANOCLAW_INSTALL_DIR", "")

# First shipped gate (resolve-only promise, finally settles). Exact bytes workers may still have.
PREVIOUS_PATCHED_GATE = """let resolveStartupAdoption!: () => void;
/** Argus: settles once startup adoption has returned or thrown (patch-startup-adoption-gate.py). */
export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve) => {
  resolveStartupAdoption = resolve;
});

export async function adoptRunningSessions(): Promise<{ adopted: number; stopped: number }> {
  try {
    return await adoptRunningSessionsOnce();
  } finally {
    resolveStartupAdoption();
  }
}

async function adoptRunningSessionsOnce(): Promise<{ adopted: number; stopped: number }> {
"""

# Broken shape left by upgrading the previous patch with an older script (duplicate Once header).
BROKEN_UPGRADE_SHAPE = PREVIOUS_PATCHED_GATE.replace(
    """  try {
    return await adoptRunningSessionsOnce();
  } finally {
    resolveStartupAdoption();
  }
""",
    """  try {
    const result = await adoptRunningSessionsOnce();
    resolveStartupAdoption();
    return result;
  } catch (error) {
    rejectStartupAdoption(error);
    throw error;
  }
""",
    1,
).replace(
    "async function adoptRunningSessionsOnce(): Promise<{ adopted: number; stopped: number }> {\n",
    "async function adoptRunningSessionsOnce(): Promise<{ adopted: number; stopped: number }> {\n\n"
    "async function adoptRunningSessionsOnce(): Promise<{ adopted: number; stopped: number }> {\n",
    1,
)


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


def _assert_canonical(text: str) -> None:
    assert text.count("export const startupAdoptionDone") == 1
    assert text.count("export async function adoptRunningSessions()") == 1
    assert text.count("async function adoptRunningSessionsOnce()") == 1
    assert "let rejectStartupAdoption!: (reason?: unknown) => void;" in text
    assert "rejectStartupAdoption(error)" in text
    assert "resolveStartupAdoption();" in text
    assert "finally {\n    resolveStartupAdoption();" not in text
    if "Failed to list existing sessions for adoption" in text:
        assert LIST_FAIL_CLOSED in text
        assert LIST_FAIL_SOFT not in text


def test_wraps_once_and_is_idempotent(tmp_path: Path) -> None:
    root = _checkout(tmp_path, f"// before\n{HEAD}  return x;\n}}\n")
    assert _run(root).returncode == 0
    text = (root / "src" / "container-runner.ts").read_text()
    _assert_canonical(text)
    assert _run(root).returncode == 0
    assert (root / "src" / "container-runner.ts").read_text() == text


def test_upgrades_the_old_finally_only_gate(tmp_path: Path) -> None:
    body = f"// before\n{PREVIOUS_PATCHED_GATE}  return {{ adopted: 0, stopped: 0 }};\n}}\n"
    root = _checkout(tmp_path, body)
    assert _run(root).returncode == 0
    assert _run(root).returncode == 0
    text = (root / "src" / "container-runner.ts").read_text()
    _assert_canonical(text)


def test_fail_closed_listing_upgrades_on_an_already_wrapped_gate(
    tmp_path: Path,
) -> None:
    body = f"""// before
let resolveStartupAdoption!: () => void;
let rejectStartupAdoption!: (reason?: unknown) => void;
export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve, reject) => {{
  resolveStartupAdoption = resolve;
  rejectStartupAdoption = reject;
}});

export async function adoptRunningSessions(): Promise<{{ adopted: number; stopped: number }}> {{
  try {{
    const result = await adoptRunningSessionsOnce();
    resolveStartupAdoption();
    return result;
  }} catch (error) {{
    rejectStartupAdoption(error);
    throw error;
  }}
}}

async function adoptRunningSessionsOnce(): Promise<{{ adopted: number; stopped: number }}> {{
  const driver = getSessionDriver();
  let snapshots: SupervisedSnapshot[];
  try {{
    snapshots = await driver.listSessions(INSTALL_SLUG);
{LIST_FAIL_SOFT}
}}
"""
    root = _checkout(tmp_path, body)
    assert _run(root).returncode == 0
    text = (root / "src" / "container-runner.ts").read_text()
    assert LIST_FAIL_CLOSED in text
    assert LIST_FAIL_SOFT not in text


def test_repairs_duplicate_once_from_broken_upgrade(tmp_path: Path) -> None:
    body = (
        f"// before\n{BROKEN_UPGRADE_SHAPE}  return {{ adopted: 0, stopped: 0 }};\n}}\n"
    )
    root = _checkout(tmp_path, body)
    assert _run(root).returncode == 0
    _assert_canonical((root / "src" / "container-runner.ts").read_text())


def test_fails_closed_on_unknown_gate_shape(tmp_path: Path) -> None:
    body = """// before
export const startupAdoptionDone: Promise<void> = Promise.resolve();
export async function adoptRunningSessions(): Promise<{ adopted: number; stopped: number }> {
  return { adopted: 0, stopped: 0 };
}
"""
    root = _checkout(tmp_path, body)
    result = _run(root)
    assert result.returncode != 0
    assert "installed shape unrecognized" in (result.stderr + result.stdout)


@pytest.mark.parametrize("body", ["// nothing here\n", HEAD + HEAD])
def test_fails_closed_when_the_anchor_is_missing_or_ambiguous(
    tmp_path: Path, body: str
) -> None:
    root = _checkout(tmp_path, body)
    result = _run(root)
    assert result.returncode != 0
    assert "upstream shape changed" in (result.stderr + result.stdout)
    assert (root / "src" / "container-runner.ts").read_text() == body


def _nanoclaw_git_dir() -> Path | None:
    if NANOCLAW_INSTALL:
        root = Path(NANOCLAW_INSTALL)
        if (root / ".git").is_dir() and (root / "node_modules").is_dir():
            return root
    return None


def _copy_nanoclaw_for_build(git_dir: Path, work: Path) -> None:
    """Disposable tree for `pnpm run build`; pristine src at PINNED, shared node_modules."""
    work.mkdir(parents=True, exist_ok=True)
    for name in (
        "package.json",
        "pnpm-lock.yaml",
        "tsconfig.json",
        "tsconfig.build.json",
    ):
        src = git_dir / name
        if src.is_file():
            shutil.copy2(src, work / name)
    archive = subprocess.run(
        ["git", "-C", str(git_dir), "archive", "--format=tar", PINNED, "src"],
        capture_output=True,
        check=True,
        timeout=60,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(work)
    nm = work / "node_modules"
    if not nm.exists():
        nm.symlink_to(git_dir / "node_modules", target_is_directory=True)


def _pristine_container_runner(git_dir: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(git_dir), "show", f"{PINNED}:src/container-runner.ts"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return proc.stdout


def _tsc_nanoclaw(work: Path) -> None:
    proc = subprocess.run(
        ["pnpm", "run", "build"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(
    not _nanoclaw_git_dir(), reason="ARGUS_NANOCLAW_INSTALL_DIR git checkout"
)
def test_pristine_patch_twice_typechecks(tmp_path: Path) -> None:
    git_dir = _nanoclaw_git_dir()
    assert git_dir is not None
    work = tmp_path / "nc"
    _copy_nanoclaw_for_build(git_dir, work)
    runner = work / "src" / "container-runner.ts"
    runner.write_text(_pristine_container_runner(git_dir))
    assert _run(work).returncode == 0
    assert _run(work).returncode == 0
    _assert_canonical(runner.read_text())
    _tsc_nanoclaw(work)


@pytest.mark.skipif(
    not _nanoclaw_git_dir(), reason="ARGUS_NANOCLAW_INSTALL_DIR git checkout"
)
def test_previous_shape_patch_twice_typechecks(tmp_path: Path) -> None:
    git_dir = _nanoclaw_git_dir()
    assert git_dir is not None
    pristine = _pristine_container_runner(git_dir)
    marker = HEAD
    assert marker in pristine
    patched_prev = pristine.replace(
        marker,
        PREVIOUS_PATCHED_GATE,
        1,
    )
    work = tmp_path / "nc"
    _copy_nanoclaw_for_build(git_dir, work)
    runner = work / "src" / "container-runner.ts"
    runner.write_text(patched_prev)
    assert _run(work).returncode == 0
    assert _run(work).returncode == 0
    _assert_canonical(runner.read_text())
    _tsc_nanoclaw(work)
