import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "check_paths.py"
REAL_ROOT = SCRIPT.parent.parent
POLICY = REAL_ROOT / "scripts" / "path_policy.json"


def clean_env() -> dict[str, str]:
    """The caller's environment without GIT_* (a hook's), so git acts on the temp repo."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def run_git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ("git", "-C", str(repo), *args),
        capture_output=True,
        text=True,
        check=True,
        env=clean_env(),
    )
    return done.stdout


def write(repo: Path, name: str, text: str | bytes = "x\n") -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)


def commit(repo: Path, message: str = "change") -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose `main` holds an existing stray file, with a branch `work` off it."""
    run_git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "README.md")
    write(tmp_path, "old-stray.txt")  # already there: never judged
    write(tmp_path, "docs/runbooks/bad-name.md")  # already there: never judged
    commit(tmp_path, "base")
    run_git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    run_git(tmp_path, "checkout", "-q", "-b", "work")
    return tmp_path


def check(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "--root",
            str(repo),
            "--policy",
            str(POLICY),
            *args,
        ),
        capture_output=True,
        text=True,
        check=False,
    )


def violations(repo: Path) -> dict[str, set[str]]:
    done = check(repo, "--json")
    out: dict[str, set[str]] = {}
    for v in json.loads(done.stdout):
        out.setdefault(v["path"], set()).add(v["rule"])
    return out


def test_stray_file_at_the_root_fails_with_the_fix(repo: Path) -> None:
    write(repo, "pr-body.txt")
    commit(repo)
    done = check(repo)
    assert done.returncode == 1
    lines = done.stdout.splitlines()
    assert any(line.startswith("pr-body.txt: root-file: ") for line in lines)
    assert any(line.startswith("pr-body.txt: deny-pattern: ") for line in lines)
    assert "/tmp" in done.stdout


def test_allowed_root_file_and_known_types_pass(repo: Path) -> None:
    write(repo, "CONTRIBUTING.md")
    write(repo, "scripts/tool.py")
    write(repo, "docs/runbooks/2026-11-18.some-slug.ref.md")
    write(repo, "docs/specs/some-slug.spec.md")
    commit(repo)
    done = check(repo)
    assert (done.returncode, done.stdout) == (0, "")


@pytest.mark.parametrize(
    "name",
    [
        "docs/x/pr-body.md",
        "scripts/run-body.md",
        "scripts/.argus-deliver-body.md",
        "scripts/out.log",
        "scripts/a.py.bak",
        "scripts/a.py~",
        "scripts/nohup.out",
        "scripts/core.1234",
        "scripts/scratch.py",
        "scripts/notes-1.txt",
        "apps/api/src/__pycache__/a.py",
        "apps/dashboard/src/node_modules/a.ts",
        "scripts/.venv/a.py",
    ],
)
def test_deny_patterns_fail_anywhere(repo: Path, name: str) -> None:
    write(repo, name)
    commit(repo)
    assert "deny-pattern" in violations(repo)[name]


def test_extension_not_allowed_for_the_directory_fails(repo: Path) -> None:
    write(repo, "docs/diagram.txt")
    write(repo, "scripts/helper.ts")  # allowed
    write(repo, "scripts/helper.rb")
    write(repo, "newdir/a.py")
    commit(repo)
    found = violations(repo)
    assert "extension" in found["docs/diagram.txt"]
    assert found["scripts/helper.rb"] == {"extension"}
    assert found["newdir/a.py"] == {"unknown-dir"}
    assert "scripts/helper.ts" not in found


def test_docs_names_follow_the_documented_conventions(repo: Path) -> None:
    write(repo, "docs/runbooks/my-runbook.md")
    write(repo, "docs/runbooks/2026-11-18.Bad_Slug.ref.md")
    write(repo, "docs/specs/2026-11-18.dated.spec.md")
    write(repo, "docs/drafts/2026-11-18.idea.ref.md")
    write(repo, "docs/notes.md")
    commit(repo)
    found = violations(repo)
    for name in (
        "docs/runbooks/my-runbook.md",
        "docs/runbooks/2026-11-18.Bad_Slug.ref.md",
        "docs/specs/2026-11-18.dated.spec.md",
        "docs/drafts/2026-11-18.idea.ref.md",
        "docs/notes.md",
    ):
        assert found[name] == {"name-convention"}, name


def test_big_files_and_binaries_fail(repo: Path) -> None:
    write(repo, "scripts/big.json", "0" * (512 * 1024 + 1))
    write(repo, "assets/big.png", b"\0" * (512 * 1024 + 1))
    write(repo, "scripts/blob.json", b"\0\1\2")
    write(repo, "assets/ok.png", b"\0\1\2")
    commit(repo)
    found = violations(repo)
    assert found["scripts/big.json"] == {"size"}
    assert found["assets/big.png"] == {"size"}
    assert found["scripts/blob.json"] == {"binary"}
    assert "assets/ok.png" not in found


def test_large_files_list_exempts_a_path(repo: Path, tmp_path_factory) -> None:
    policy = json.loads(POLICY.read_text())
    policy["large_files"].append("scripts/big.json")
    custom = tmp_path_factory.mktemp("policy") / "policy.json"
    custom.write_text(json.dumps(policy))
    write(repo, "scripts/big.json", "0" * (512 * 1024 + 1))
    commit(repo)
    done = subprocess.run(
        (sys.executable, str(SCRIPT), "--root", str(repo), "--policy", str(custom)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "")


def test_a_rename_is_judged_at_its_new_path(repo: Path) -> None:
    (repo / "scripts").mkdir()
    run_git(repo, "mv", "old-stray.txt", "scripts/old-stray.txt")
    run_git(repo, "mv", "README.md", "pr-body.txt")
    commit(repo)
    found = violations(repo)
    assert found["pr-body.txt"] == {"root-file", "deny-pattern"}
    assert "scripts/old-stray.txt" in found  # .txt is not a scripts/ type
    assert "old-stray.txt" not in found


def test_existing_files_are_not_judged(repo: Path) -> None:
    write(repo, "old-stray.txt", "edited\n")
    write(repo, "docs/runbooks/bad-name.md", "edited\n")
    commit(repo)
    done = check(repo)
    assert (done.returncode, done.stdout) == (0, "")
    assert check(repo, "--all").returncode == 1  # --all does judge them


def test_policy_edit_prints_the_notice(repo: Path) -> None:
    write(repo, "scripts/path_policy.json", "{}\n")
    commit(repo)
    done = check(repo)
    assert done.returncode == 0
    assert "policy changed" in done.stdout


def test_json_shape(repo: Path) -> None:
    write(repo, "pr-body.txt")
    commit(repo)
    done = check(repo, "--json")
    assert done.returncode == 1
    found = json.loads(done.stdout)
    assert all(set(v) == {"path", "rule", "action"} for v in found)
    assert {v["rule"] for v in found} == {"root-file", "deny-pattern"}
    clean = subprocess.run(
        (sys.executable, str(SCRIPT), "--root", str(REAL_ROOT), "--all", "--json"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert json.loads(clean.stdout) == []


def test_missing_base_is_an_error_not_a_pass(repo: Path) -> None:
    done = check(repo, "--base", "origin/nope")
    assert done.returncode == 2
    assert "fetch the base" in done.stderr


def test_the_real_tree_has_no_violations() -> None:
    done = subprocess.run(
        (sys.executable, str(SCRIPT), "--all"),
        cwd=REAL_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=clean_env(),
    )
    assert (done.returncode, done.stdout) == (0, "")
