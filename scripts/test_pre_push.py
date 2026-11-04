"""The pre-push hook runs its checks outside git's hook environment.

Git runs hooks with GIT_DIR (and, for a worktree, GIT_INDEX_FILE and friends)
pointing at the real repository. A test that runs `git init` or `git commit` in a
temporary directory then acts on the real repository instead
(docs/postmortems/2026-11-04.pre-push-git-env.ref.md).
"""

import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-push"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_checks_see_no_git_location_variables(tmp_path: Path) -> None:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True, env=clean)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "first"],
        cwd=work, check=True, env=clean,
    )  # fmt: skip
    head = _git(work, "rev-parse", "HEAD")
    # A stand-in `make` records what the checks would inherit.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen"
    make = bin_dir / "make"
    make.write_text(f"#!/bin/sh\nenv | grep '^GIT_' > {seen}\n")
    make.chmod(0o755)
    hook_env = {
        **clean,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        # What git exports to a hook in a worktree.
        "GIT_DIR": str(work / ".git"),
        "GIT_INDEX_FILE": str(work / ".git" / "index"),
        "GIT_WORK_TREE": str(work),
    }
    done = subprocess.run(
        [str(HOOK), "origin", "url"],
        cwd=work,
        env=hook_env,
        input=f"refs/heads/main {head} refs/heads/main {'0' * 40}\n",
        capture_output=True,
        text=True,
    )
    assert seen.exists(), done.stderr
    assert seen.read_text() == ""
