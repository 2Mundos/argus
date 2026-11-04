"""Commit-attribution enforcement (worker-lifecycle 3.8, commit_attribution.py)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parents[1] / "engines"
MODULE = ENGINES / "commit_attribution.py"
PRODUCTION_HOOKS = "/usr/local/lib/argus/git-hooks"
PRODUCTION_GIT = "/usr/local/lib/argus/bin/git"

spec = importlib.util.spec_from_file_location("commit_attribution", MODULE)
assert spec and spec.loader
attribution = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = attribution
spec.loader.exec_module(attribution)

GUARD = ENGINES / "command_guard.py"
gspec = importlib.util.spec_from_file_location("command_guard", GUARD)
assert gspec and gspec.loader
guard = importlib.util.module_from_spec(gspec)
gspec.loader.exec_module(guard)

ARGUS_GIT = ENGINES / "argus-git"
HOOK = ENGINES / "git-hooks" / "commit-msg"
REAL = shutil.which("git") or "/usr/bin/git"


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "ARGUS_ATTRIBUTION_TEST_HOOKS": "1",
        "ARGUS_GIT_REAL": REAL,
        "ARGUS_COMMIT_ATTRIBUTION_PY": str(MODULE),
    }


def _run_git(
    cwd: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ARGUS_GIT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "message",
    [
        "feat: ok\n\nSigned-off-by: alice@example.com",
        "fix: mention Co-Authored-By in prose without a trailer line",
        "Subject only",
    ],
)
def test_allows_normal_messages(message: str) -> None:
    assert attribution.check_message(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "feat: x\n\nCo-Authored-By: Bot <bot@example.com>",
        "Co-authored-by: someone",
        "Subject\n\nHelped-by: alice",
        "Subject\n\nCO-AUTHORED-BY: Bot <b@e.com>",
    ],
)
def test_rejects_attribution_trailers(message: str) -> None:
    assert attribution.check_message(message) is not None


def test_workspace_hooks_path_matches_image() -> None:
    workspace = (Path(__file__).resolve().parents[1] / "argus-workspace.ts").read_text()
    assert 'WORKER_GIT_HOOKS_PATH = "/usr/local/lib/argus/git-hooks"' in workspace
    assert PRODUCTION_HOOKS == "/usr/local/lib/argus/git-hooks"


def test_commit_argv_with_message_flag() -> None:
    assert attribution.check_commit_argv(["commit", "-m", "ok"]) is None
    err = attribution.check_commit_argv(["commit", "-m", "x\n\nCo-Authored-By: a"])
    assert err and "Co-Authored-By" in err


def test_bash_git_commit_blocked_by_command_guard() -> None:
    assert guard.check('git commit -m "x\n\nCo-Authored-By: a"') == "commit_attribution"


def test_git_c_global_options(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([REAL, "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=repo, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=repo, check=True)
    (repo / "a").write_text("1\n")
    subprocess.run([REAL, "add", "a"], cwd=repo, check=True)
    env = _env()
    bad = _run_git(repo, env, "-C", str(repo), "commit", "-m", "x\n\nCo-Authored-By: z")
    assert bad.returncode != 0
    good = _run_git(repo, env, "-C", str(repo), "commit", "-m", "ok")
    assert good.returncode == 0, good.stderr


def test_push_all_fails_closed(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run([REAL, "init", "-q"], cwd=repo, check=True)
    err = attribution.check_push(str(repo), ["git", "push", "--all"])
    assert err and "--all" in err


def test_push_with_unreachable_remote_fails_closed(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run([REAL, "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [REAL, "remote", "add", "origin", "https://127.0.0.1:9/nope.git"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=repo, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=repo, check=True)
    (repo / "f").write_text("x\n")
    subprocess.run([REAL, "add", "f"], cwd=repo, check=True)
    subprocess.run([REAL, "commit", "-m", "ok"], cwd=repo, check=True)
    err = attribution.check_push(str(repo), ["git", "push", "-u", "origin", "main"])
    assert err and err.startswith("Argus")


def test_new_branch_push_ignores_old_bad_on_main(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run([REAL, "init", "--bare", str(bare)], check=True, capture_output=True)
    origin = tmp_path / "origin"
    subprocess.run(
        [REAL, "clone", str(bare), str(origin)], check=True, capture_output=True
    )
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=origin, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=origin, check=True)
    (origin / "f").write_text("1\n")
    subprocess.run([REAL, "add", "f"], cwd=origin, check=True)
    subprocess.run(
        [REAL, "commit", "--no-verify", "-m", "legacy\n\nCo-Authored-By: old"],
        cwd=origin,
        check=True,
    )
    subprocess.run([REAL, "branch", "-M", "main"], cwd=origin, check=True)
    subprocess.run([REAL, "push", "-u", "origin", "main"], cwd=origin, check=True)
    subprocess.run([REAL, "checkout", "-b", "feature"], cwd=origin, check=True)
    (origin / "f").write_text("2\n")
    subprocess.run([REAL, "add", "f"], cwd=origin, check=True)
    subprocess.run([REAL, "commit", "-m", "new work"], cwd=origin, check=True)
    env = _env()
    push = _run_git(origin, env, "push", "-u", "origin", "feature")
    assert push.returncode == 0, push.stderr


def test_git_wrapper_hook_and_outgoing_push(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run([REAL, "init", "--bare", str(bare)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(
        [REAL, "clone", str(bare), str(clone)], check=True, capture_output=True
    )
    branch = subprocess.run(
        [REAL, "symbolic-ref", "--short", "HEAD"],
        cwd=clone,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=clone, check=True
    )
    subprocess.run([REAL, "config", "user.name", "Worker"], cwd=clone, check=True)
    subprocess.run(
        [REAL, "config", "core.hooksPath", str(ENGINES / "git-hooks")],
        cwd=clone,
        check=True,
    )
    env = _env()
    msg = tmp_path / "msg"
    msg.write_text("bad\n\nCo-Authored-By: x\n")
    hook = subprocess.run(
        [str(HOOK), str(msg)], env=env, capture_output=True, text=True
    )
    assert hook.returncode != 0
    msg.write_text("ok\n")
    assert subprocess.run([str(HOOK), str(msg)], env=env).returncode == 0

    (clone / "f").write_text("a\n")
    subprocess.run([REAL, "add", "f"], cwd=clone, check=True)
    bad = _run_git(clone, env, "commit", "-m", "bad\n\nCo-Authored-By: x")
    assert bad.returncode != 0
    good = _run_git(clone, env, "commit", "-m", "good")
    assert good.returncode == 0, good.stderr
    assert _run_git(clone, env, "push", "-u", "origin", branch).returncode == 0

    (clone / "f").write_text("b\n")
    subprocess.run([REAL, "add", "f"], cwd=clone, check=True)
    subprocess.run(
        [REAL, "commit", "--no-verify", "-m", "second\n\nCo-Authored-By: y"],
        cwd=clone,
        check=True,
    )
    push2 = _run_git(clone, env, "push", "origin", branch)
    assert push2.returncode != 0
    assert "Argus rejects" in push2.stderr


def test_divergent_push_rejects_only_outgoing_bad_commit(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run([REAL, "init", "--bare", str(bare)], check=True, capture_output=True)
    origin = tmp_path / "origin"
    subprocess.run(
        [REAL, "clone", str(bare), str(origin)], check=True, capture_output=True
    )
    subprocess.run([REAL, "config", "user.email", "a@e.com"], cwd=origin, check=True)
    subprocess.run([REAL, "config", "user.name", "A"], cwd=origin, check=True)
    (origin / "f").write_text("1\n")
    subprocess.run([REAL, "add", "f"], cwd=origin, check=True)
    subprocess.run([REAL, "commit", "-m", "base"], cwd=origin, check=True)
    subprocess.run([REAL, "branch", "-M", "main"], cwd=origin, check=True)
    subprocess.run([REAL, "push", "-u", "origin", "main"], cwd=origin, check=True)
    subprocess.run(
        [REAL, "clone", str(bare), str(tmp_path / "worker")],
        check=True,
        capture_output=True,
    )
    worker = tmp_path / "worker"
    subprocess.run([REAL, "pull", "origin", "main"], cwd=worker, check=True)
    subprocess.run([REAL, "config", "user.email", "w@e.com"], cwd=worker, check=True)
    subprocess.run([REAL, "config", "user.name", "W"], cwd=worker, check=True)
    (worker / "f").write_text("2\n")
    subprocess.run([REAL, "add", "f"], cwd=worker, check=True)
    subprocess.run(
        [REAL, "commit", "--no-verify", "-m", "bad\n\nCo-Authored-By: diverge"],
        cwd=worker,
        check=True,
    )
    env = _env()
    push = _run_git(worker, env, "push", "origin", "main")
    assert push.returncode != 0


def test_real_git_ignores_env_override_without_test_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARGUS_ATTRIBUTION_TEST_HOOKS", raising=False)
    monkeypatch.setenv("ARGUS_GIT_REAL", "/tmp/evil-git")
    assert attribution.real_git() == "/usr/bin/git"


def test_git_dir_without_work_tree_fails_closed(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([REAL, "init", "-q"], cwd=repo, check=True)
    env = _env()
    bad = _run_git(
        repo,
        env,
        "--git-dir",
        str(repo / ".git"),
        "commit",
        "-m",
        "ok",
    )
    assert bad.returncode != 0
    assert "work-tree" in bad.stderr


def test_push_multiple_refspecs_and_alternate_remote(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run([REAL, "init", "--bare", str(bare)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(
        [REAL, "clone", str(bare), str(clone)], check=True, capture_output=True
    )
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=clone, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=clone, check=True)
    subprocess.run([REAL, "checkout", "-b", "main"], cwd=clone, check=True)
    (clone / "f").write_text("1\n")
    subprocess.run([REAL, "add", "f"], cwd=clone, check=True)
    subprocess.run([REAL, "commit", "-m", "base"], cwd=clone, check=True)
    subprocess.run([REAL, "checkout", "-b", "feature"], cwd=clone, check=True)
    (clone / "f").write_text("2\n")
    subprocess.run([REAL, "add", "f"], cwd=clone, check=True)
    subprocess.run([REAL, "commit", "-m", "feature work"], cwd=clone, check=True)
    subprocess.run(
        [REAL, "remote", "add", "upstream", str(bare)], cwd=clone, check=True
    )
    env = _env()
    push = _run_git(
        clone,
        env,
        "push",
        "-u",
        "upstream",
        "main",
        "feature",
    )
    assert push.returncode == 0, push.stderr


def test_wrapper_rejects_commit_no_verify(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([REAL, "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=repo, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=repo, check=True)
    (repo / "a").write_text("1\n")
    subprocess.run([REAL, "add", "a"], cwd=repo, check=True)
    env = _env()
    bad = _run_git(repo, env, "commit", "--no-verify", "-m", "ok")
    assert bad.returncode != 0
    assert "no-verify" in bad.stderr


def test_tag_push_fails_when_commit_already_on_remote_main(tmp_path: Path) -> None:
    """Reproduce review bypass: tag refspec must not skip attribution scanning."""
    if not REAL:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run([REAL, "init", "--bare", str(bare)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(
        [REAL, "clone", str(bare), str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=repo, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=repo, check=True)
    subprocess.run([REAL, "checkout", "-b", "main"], cwd=repo, check=True)
    (repo / "f").write_text("1\n")
    subprocess.run([REAL, "add", "f"], cwd=repo, check=True)
    subprocess.run(
        [REAL, "commit", "--no-verify", "-m", "legacy\n\nCo-Authored-By: on-main"],
        cwd=repo,
        check=True,
    )
    subprocess.run([REAL, "push", "-u", "origin", "main"], cwd=repo, check=True)
    subprocess.run([REAL, "tag", "vbad"], cwd=repo, check=True)
    env = _env()
    tag_push = _run_git(repo, env, "push", "origin", "vbad")
    assert tag_push.returncode != 0
    assert "non-branch" in tag_push.stderr.lower() or "tag" in tag_push.stderr.lower()

    subprocess.run([REAL, "checkout", "-b", "feature"], cwd=repo, check=True)
    (repo / "f").write_text("2\n")
    subprocess.run([REAL, "add", "f"], cwd=repo, check=True)
    subprocess.run([REAL, "commit", "-m", "clean feature work"], cwd=repo, check=True)
    branch_push = _run_git(repo, env, "push", "-u", "origin", "feature")
    assert branch_push.returncode == 0, branch_push.stderr


def test_annotated_tag_push_fails_closed(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run([REAL, "init", "--bare", str(bare)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(
        [REAL, "clone", str(bare), str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=repo, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=repo, check=True)
    subprocess.run([REAL, "checkout", "-b", "main"], cwd=repo, check=True)
    (repo / "f").write_text("1\n")
    subprocess.run([REAL, "add", "f"], cwd=repo, check=True)
    subprocess.run([REAL, "commit", "-m", "ok"], cwd=repo, check=True)
    subprocess.run([REAL, "tag", "-a", "vannot", "-m", "release"], cwd=repo, check=True)
    env = _env()
    push = _run_git(repo, env, "push", "origin", "vannot")
    assert push.returncode != 0


def test_push_follow_tags_fails_closed(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run([REAL, "init", "--bare", str(bare)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(
        [REAL, "clone", str(bare), str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=repo, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=repo, check=True)
    subprocess.run([REAL, "checkout", "-b", "main"], cwd=repo, check=True)
    (repo / "f").write_text("1\n")
    subprocess.run([REAL, "add", "f"], cwd=repo, check=True)
    subprocess.run([REAL, "commit", "-m", "ok"], cwd=repo, check=True)
    subprocess.run([REAL, "tag", "v1"], cwd=repo, check=True)
    env = _env()
    push = _run_git(repo, env, "push", "-u", "origin", "main", "--follow-tags")
    assert push.returncode != 0
    assert "follow-tags" in push.stderr


def test_wrapper_rejects_core_hooks_path_override(tmp_path: Path) -> None:
    if not REAL:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([REAL, "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [REAL, "config", "user.email", "w@example.com"], cwd=repo, check=True
    )
    subprocess.run([REAL, "config", "user.name", "W"], cwd=repo, check=True)
    subprocess.run(
        [REAL, "config", "core.hooksPath", str(ENGINES / "git-hooks")],
        cwd=repo,
        check=True,
    )
    (repo / "a").write_text("1\n")
    subprocess.run([REAL, "add", "a"], cwd=repo, check=True)
    env = _env()
    with_msg = _run_git(
        repo,
        env,
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "ok",
    )
    assert with_msg.returncode != 0
    assert "hooksPath" in with_msg.stderr
    editor = _run_git(repo, env, "-c", "core.hooksPath=/dev/null", "commit")
    assert editor.returncode != 0
    assert "hooksPath" in editor.stderr
