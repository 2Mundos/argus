import os
import subprocess
from pathlib import Path

import pytest
from release import Change, main, next_version, parse, render


def test_parse_pr_merge_keeps_title_and_number() -> None:
    c = parse(
        "a" * 40,
        "Merge pull request 'feat(accounts): usage per account' (#14) from "
        "feat/account-usage into main",
        "",
    )
    assert c == Change("a" * 40, "feat", "accounts: usage per account", 14, False)


def test_parse_github_pr_merge_takes_title_from_body() -> None:
    c = parse(
        "a" * 40,
        "Merge pull request #14 from 2Mundos/feat/account-usage",
        "feat(accounts)!: usage per account\n",
    )
    assert c == Change("a" * 40, "feat", "accounts: usage per account", 14, True)


def test_parse_skips_other_merges_and_keeps_plain_subjects() -> None:
    assert parse("s", "Merge main into feat/x", "") is None
    assert parse("s", "Tidy things", "") == Change("s", "", "Tidy things", None, False)


@pytest.mark.parametrize(
    ("subject", "body"),
    [("feat(api)!: drop v0 routes", ""), ("fix: x", "BREAKING CHANGE: y")],
)
def test_parse_breaking_markers(subject: str, body: str) -> None:
    c = parse("s", subject, body)
    assert c is not None and c.breaking


def _c(kind: str) -> Change:
    return Change("s", kind, "x", None, False)


@pytest.mark.parametrize(
    ("prev", "kinds", "breaking", "expected"),
    [
        ((0, 3, 1), ["fix"], False, (0, 3, 2)),
        ((0, 3, 1), ["fix", "feat"], False, (0, 4, 0)),
        ((0, 3, 1), ["fix"], True, (0, 4, 0)),
        ((1, 3, 1), ["fix"], True, (2, 0, 0)),
        ((1, 3, 1), ["docs"], False, (1, 3, 2)),
    ],
)
def test_next_version(
    prev: tuple[int, int, int],
    kinds: list[str],
    breaking: bool,
    expected: tuple[int, int, int],
) -> None:
    assert next_version(prev, [_c(k) for k in kinds], breaking) == expected


def test_render_groups_and_flags_migrations() -> None:
    changes = [
        Change("1" * 40, "feat", "api: a", 3, False),
        Change("2" * 40, "fix", "b", None, False),
        Change("3" * 40, "docs", "c", None, False),
    ]
    notes = render(
        "v0.2.0", "2026-10-18", "v0.1.0", "v0.1.0..abc", changes, [],
        ["0016_x.sql"], ["releases.spec.md"],
    )  # fmt: skip
    assert "- api: a (#3) (`111111111111`)" in notes
    assert notes.index("## Features") < notes.index("## Fixes")
    assert notes.index("## Fixes") < notes.index("## Maintenance")
    assert "`0016_x.sql`" in notes and "Back up the database" in notes
    assert "[`releases.spec.md`](../specs/releases.spec.md)" in notes
    assert "## Breaking" not in notes


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Under a git hook (pre-push) GIT_DIR and friends point at the real repo.
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "init", "--quiet", "-b", "main", str(work))
    for k, v in [("user.name", "t"), ("user.email", "t@t"), ("tag.gpgSign", "false"),
                 ("commit.gpgSign", "false")]:  # fmt: skip
        _git(work, "config", k, v)
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "commit", "--quiet", "--allow-empty", "-m", "feat: first")
    _git(work, "push", "--quiet", "-u", "origin", "main")
    return work


def test_release_end_to_end(repo: Path) -> None:
    assert main(["--date", "2026-10-18", "--push"], root=repo) == 0
    assert _git(repo, "describe", "--tags") == "v0.1.0"
    assert (repo / "docs/releases/2026-10-18.v0-1-0.ref.md").exists()
    assert _git(repo, "ls-remote", "--tags", "origin", "v0.1.0")

    mig = repo / "apps/api/src/argus_api/db/migrations"
    mig.mkdir(parents=True)
    (mig / "0001_init.sql").write_text("")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "fix(db): add a table")
    _git(repo, "push", "--quiet")
    assert main(["--date", "2026-10-19"], root=repo) == 0
    notes = (repo / "docs/releases/2026-10-19.v0-1-1.ref.md").read_text()
    assert "`0001_init.sql`" in notes and "- db: add a table" in notes
    assert "docs(release)" not in notes


def test_release_refuses_unpublished_head(repo: Path) -> None:
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "fix: local only")
    assert main(["--date", "2026-10-18"], root=repo) == 1
    assert _git(repo, "tag") == ""
