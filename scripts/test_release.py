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
    assert (
        "[`releases.spec.md`](https://github.com/2Mundos/argus/blob/v0.2.0/"
        "docs/specs/releases.spec.md)"
    ) in notes
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
    for k, v in [("tag.gpgSign", "false"), ("commit.gpgSign", "false")]:
        _git(work, "config", k, v)
    with (work / ".git" / "config").open("a") as f:
        f.write("[user]\n\tname = t\n\temail = t@t\n")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "commit", "--quiet", "--allow-empty", "-m", "feat: first")
    _git(work, "push", "--quiet", "-u", "origin", "main")
    return work


def _merge_notes(repo: Path, branch: str, tag: str) -> None:
    _git(repo, "fetch", "origin", branch)
    _git(repo, "merge", "--squash", "FETCH_HEAD")
    _git(repo, "commit", "--quiet", "-m", f"docs(release): {tag}")
    _git(repo, "push", "--quiet")


def test_release_end_to_end(repo: Path) -> None:
    assert main(["--date", "2026-10-18", "--no-pr"], root=repo) == 0
    assert _git(repo, "tag") == ""
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "rev-parse", "main") == _git(repo, "rev-parse", "origin/main")
    assert not (repo / "docs/releases/2026-10-18.v0-1-0.ref.md").exists()
    notes = _git(repo, "show", "release/v0.1.0:docs/releases/2026-10-18.v0-1-0.ref.md")
    assert notes.startswith("# v0.1.0")
    assert _git(repo, "ls-remote", "--heads", "origin", "release/v0.1.0")

    _merge_notes(repo, "release/v0.1.0", "v0.1.0")
    assert main(["--tag"], root=repo) == 0
    assert _git(repo, "describe", "--tags") == "v0.1.0"
    assert _git(repo, "ls-remote", "--tags", "origin", "v0.1.0")
    assert _git(repo, "rev-parse", "v0.1.0^{commit}") == _git(
        repo, "rev-parse", "origin/main"
    )

    mig = repo / "apps/api/src/argus_api/db/migrations"
    mig.mkdir(parents=True)
    (mig / "0001_init.sql").write_text("")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "fix(db): add a table")
    _git(repo, "push", "--quiet")
    assert main(["--date", "2026-10-19", "--no-pr"], root=repo) == 0
    _merge_notes(repo, "release/v0.1.1", "v0.1.1")
    assert main(["--tag"], root=repo) == 0
    notes = (repo / "docs/releases/2026-10-19.v0-1-1.ref.md").read_text()
    assert "`0001_init.sql`" in notes and "- db: add a table" in notes
    assert "docs(release)" not in notes


def test_tag_keeps_a_later_main_commit_out_of_the_release(repo: Path) -> None:
    assert main(["--date", "2026-10-18", "--no-pr"], root=repo) == 0
    _merge_notes(repo, "release/v0.1.0", "v0.1.0")
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "fix: after the release")
    _git(repo, "push", "--quiet")
    assert main(["--tag"], root=repo) == 0
    tagged = _git(repo, "rev-parse", "v0.1.0^{commit}")
    assert tagged != _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "show", "--format=", "--name-only", tagged) == (
        "docs/releases/2026-10-18.v0-1-0.ref.md"
    )


def test_prepare_refuses_a_merged_but_untagged_release(repo: Path) -> None:
    assert main(["--date", "2026-10-18", "--no-pr"], root=repo) == 0
    _merge_notes(repo, "release/v0.1.0", "v0.1.0")
    assert main(["--date", "2026-10-19"], root=repo) == 1
    assert _git(repo, "tag") == ""


def test_prepare_ignores_untagged_notes_older_than_the_newest_tag(repo: Path) -> None:
    assert main(["--date", "2026-10-18", "--no-pr"], root=repo) == 0
    _merge_notes(repo, "release/v0.1.0", "v0.1.0")
    assert main(["--tag"], root=repo) == 0
    old = repo / "docs/releases/2026-10-17.v0-0-1.ref.md"
    old.write_text("# v0.0.1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "docs: old notes")
    _git(repo, "push", "--quiet")
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "fix: later")
    _git(repo, "push", "--quiet")
    assert main(["--date", "2026-10-19", "--no-pr"], root=repo) == 0
    assert _git(repo, "ls-remote", "--heads", "origin", "release/v0.1.1")


def test_push_is_retired(repo: Path) -> None:
    assert main(["--push"], root=repo) == 1
    assert _git(repo, "tag") == ""
    assert _git(repo, "rev-parse", "main") == _git(repo, "rev-parse", "origin/main")


def test_release_refuses_unpublished_head(repo: Path) -> None:
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "fix: local only")
    assert main(["--date", "2026-10-18"], root=repo) == 1
    assert _git(repo, "tag") == ""
