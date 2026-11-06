import subprocess
from pathlib import Path

import pytest
from check_engine_transcripts import PINS, ROOT, TRANSCRIPTS, main

TEXT = {
    "claude": 'URL = "https://x/claude-code-releases/{v}/{{}}/claude"\n',
    "codex": 'CODEX_VERSION="{v}"\n',
    "copilot": 'VERSION="${{COPILOT_CLI_VERSION:-{v}}}"\n',
    "cursor": 'VERSION="${{CURSOR_AGENT_VERSION:-{v}}}"\n',
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
    )


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def pin(repo: Path, engine: str, version: str) -> None:
    write(repo, PINS[engine][0], TEXT[engine].format(v=version))


def case(repo: Path, engine: str, name: str) -> None:
    write(repo, f"{TRANSCRIPTS}/{engine}/{name}/events.jsonl", "{}\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    # A throwaway repo's own identity, written to its config file.
    with (tmp_path / ".git" / "config").open("a") as config:
        config.write("[user]\n\tname = t\n\temail = t@t\n")
    for engine in PINS:
        pin(tmp_path, engine, "1.0.105")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


def run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main([], repo)
    return code, capsys.readouterr().out


def test_no_bump_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(repo, capsys) == (0, "")


@pytest.mark.parametrize("engine", list(PINS))
def test_valid_bump_passes(
    repo: Path, capsys: pytest.CaptureFixture[str], engine: str
) -> None:
    pin(repo, engine, "1.0.106")
    case(repo, engine, "success-result")
    case(repo, engine, "failure-auth")
    assert run(repo, capsys) == (0, "")


@pytest.mark.parametrize(
    ("cases", "missing"),
    [
        ([], ["success", "failure"]),
        (["success-result"], ["failure"]),
        (["failure-auth"], ["success"]),
        (["other-result", "success", "failure"], ["success", "failure"]),
    ],
)
def test_bump_missing_cases_fails(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    cases: list[str],
    missing: list[str],
) -> None:
    pin(repo, "copilot", "1.0.106")
    for name in cases:
        case(repo, "copilot", name)
    code, out = run(repo, capsys)
    assert code == 1
    lines = out.splitlines()
    assert len(lines) == len(missing)
    for kind, line in zip(missing, lines, strict=True):
        assert line == (
            "engine transcripts: copilot pin 1.0.105 -> 1.0.106 but no "
            f"{kind} case re-recorded under {TRANSCRIPTS}/copilot/ "
            f"(expected {kind}-*)"
        )


def test_cases_must_belong_to_the_bumped_engine(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pin(repo, "codex", "0.2.0")
    case(repo, "claude", "success-result")
    case(repo, "claude", "failure-auth")
    code, out = run(repo, capsys)
    assert code == 1
    assert "codex pin 1.0.105 -> 0.2.0" in out


def test_committed_bump_is_diffed_against_merge_base(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pin(repo, "cursor", "2")
    case(repo, "cursor", "success-result")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "bump")
    code, out = run(repo, capsys)
    assert code == 1
    assert out.splitlines() == [
        f"engine transcripts: cursor pin 1.0.105 -> 2 but no failure case "
        f"re-recorded under {TRANSCRIPTS}/cursor/ (expected failure-*)"
    ]


def test_unparsable_pin_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo, PINS["codex"][0], "nothing here\n")
    code, out = run(repo, capsys)
    assert code == 1
    assert "codex pin not found" in out


def test_unresolvable_base_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--base", "nope"], repo)
    assert code == 1
    assert "cannot resolve base" in capsys.readouterr().out


def test_real_repo_passes(capsys: pytest.CaptureFixture[str]) -> None:
    # HEAD always resolves; origin/main is absent in shallow or fork clones.
    code = main(["--base", "HEAD"], ROOT)
    assert (code, capsys.readouterr().out) == (0, "")
