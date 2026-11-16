import subprocess
from pathlib import Path

import pytest
import validate_language
from validate_language import (
    check_file,
    check_message,
    clean_commit_message,
    detect_languages,
    main,
    parse_added_lines,
    target_kind,
)

ENGLISH = [
    "Do not de-duplicate events",
    "The de facto standard is the command line",
    "A DoS attack on the endpoint",
    "Use DOS commands from the shell",
    "Los Angeles and São Paulo are cities",
    "Chloë will do it",
    "The café serves crème brûlée",
    "The dot-com era",
    "See v1.2.3 and #123 for details",
    "Read https://example.com/docs for more",
    "The endpoint returns 404",
    "Run the command with --verbose",
    "This keeps the API contract stable",
    "Choose one of these options",
]

PORTUGUESE = [
    "Não responda a esta mensagem",
    "Vamos fazer isso depois",
    "Atualize a página e o relatório",
    "Esta configuração fica no repositório",
    "Página de relatórios",
]


@pytest.mark.parametrize("text", ENGLISH)
def test_english_prose_passes(text: str) -> None:
    assert detect_languages(text) == set()


@pytest.mark.parametrize("text", PORTUGUESE)
def test_portuguese_prose_is_flagged(text: str) -> None:
    assert detect_languages(text)


def test_parse_added_lines_ignores_removed_dashes_and_headers() -> None:
    diff = (
        "diff --git a/a.md b/a.md\n"
        "index 111..222 100644\n"
        "--- a/a.md\n"
        "+++ b/a.md\n"
        "@@ -1,2 +1,3 @@\n"
        "--- old front matter\n"
        "+-- new comment\n"
        " unchanged\n"
        "+added\n"
    )
    assert parse_added_lines(diff) == {"a.md": {1, 3}}


def test_parse_added_lines_keeps_added_content_starting_with_plus() -> None:
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- /dev/null\n"
        "+++ b/a.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "++++ a line of pluses\n"
        "+text\n"
    )
    assert parse_added_lines(diff) == {"a.txt": {1, 2}}


def test_target_kind_covers_docs_code_and_sql() -> None:
    assert target_kind("docs/specs/a.spec.md") == "docs"
    assert target_kind("scripts/a.py") == "py"
    assert target_kind("apps/dashboard/src/a.tsx") == "cstyle"
    assert target_kind("deploy/Dockerfile.api") == "hash"
    assert target_kind("apps/api/src/argus_api/db/migrations/0001_a.sql") == "sql"
    assert target_kind("assets/logo.svg") is None


def test_python_comments_and_docstrings_are_checked_but_strings_are_not() -> None:
    source = (
        '"""Página de relatórios"""\n'
        "# comentário em português\n"
        'MSG = "mensagem em português"\n'
        "\n"
        "def render():\n"
        '    """Outra página em português"""\n'
        "    return MSG\n"
    )
    violations = check_file("a.py", set(range(1, 8)), source)
    assert [v.location for v in violations] == ["a.py:1", "a.py:2", "a.py:6"]


def test_markdown_fenced_code_is_skipped() -> None:
    source = (
        "# Title\n"
        "Introdução para a página de relatórios.\n"
        "```python\n"
        "# comentário em português\n"
        "```\n"
    )
    violations = check_file("a.md", set(range(1, 6)), source)
    assert [v.location for v in violations] == ["a.md:2"]


def test_sql_comments_are_checked() -> None:
    source = (
        "CREATE TABLE t (id INTEGER);\n"
        "-- configuração da página de relatórios\n"
        "SELECT 1;\n"
    )
    violations = check_file("a.sql", {2}, source)
    assert [v.location for v in violations] == ["a.sql:2"]


def test_commit_message_cleaning_strips_prefix_and_trailers() -> None:
    raw = "feat(api): add the page\n\nRefs #12\nCo-authored-by: A <a@b.c>\n"
    assert clean_commit_message(raw) == "add the page"


def test_commit_message_cleaning_stops_at_scissors_line() -> None:
    raw = (
        "feat: add the page\n\n"
        "# ------------------------ >8 ------------------------\n"
        "diff --git a/a b/a\n"
        "+Página de relatórios para esta configuração\n"
    )
    assert clean_commit_message(raw) == "add the page"
    assert check_message("abc123", raw) == []


def test_english_commit_message_passes() -> None:
    assert (
        check_message("abc123", "feat(api): add the page\n\nBody in English.\n") == []
    )


def test_portuguese_commit_message_is_flagged() -> None:
    violations = check_message("abc123", "feat(api): página de relatórios\n")
    assert [v.location for v in violations] == ["commit abc123:L1"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")


def test_range_mode_checks_commit_messages_and_added_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True
    ).stdout.split():
        monkeypatch.delenv(key, raising=False)
    _init_repo(tmp_path)
    doc = tmp_path / "notes.md"
    doc.write_text("Original English line.\n")
    _git(tmp_path, "add", "notes.md")
    _git(tmp_path, "commit", "-q", "-m", "feat: add notes")
    doc.write_text("Original English line.\nPágina de relatórios.\n")
    _git(tmp_path, "add", "notes.md")
    _git(tmp_path, "commit", "-q", "-m", "feat: página de relatórios")

    monkeypatch.setattr(validate_language, "ROOT", tmp_path)
    assert main(["--range", "HEAD~1..HEAD"]) == 1
    out = capsys.readouterr().out
    assert "commit " in out and "notes.md:2" in out


def test_message_file_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("fix: página de relatórios\n")
    assert main(["--message-file", str(message)]) == 1
    assert "página de relatórios" in capsys.readouterr().out


def test_message_file_mode_passes_english(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("fix: add the reports page\n")
    assert main(["--message-file", str(message)]) == 0
    assert "All new prose is English" in capsys.readouterr().out


def test_trailing_comments_are_checked_but_code_on_the_line_is_not() -> None:
    py = 'MSG = "mensagem em português para esta página"  # english note\nx = 1  # página de relatórios\n'
    assert [v.location for v in check_file("a.py", {1, 2}, py)] == ["a.py:2"]
    ts = (
        'const u = "https://example.com/página/relatórios"; // fine\n'
        "const x = 1; // configuração da página de relatórios\n"
    )
    assert [v.location for v in check_file("a.ts", {1, 2}, ts)] == ["a.ts:2"]
    sql = "SELECT 1; -- configuração da página de relatórios\n"
    assert [v.location for v in check_file("a.sql", {1}, sql)] == ["a.sql:1"]
    sh = "run_it  # configuração da página de relatórios\n"
    assert [v.location for v in check_file("a.sh", {1}, sh)] == ["a.sh:1"]


def test_cstyle_star_line_outside_a_block_comment_is_code() -> None:
    source = "const total = a\n  * página de relatórios para esta configuração\n"
    assert check_file("a.ts", {2}, source) == []
    block = "/*\n * página de relatórios para esta configuração\n */\n"
    assert [v.location for v in check_file("a.ts", {2}, block)] == ["a.ts:2"]
