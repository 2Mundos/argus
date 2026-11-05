from pathlib import Path

import pytest
from check_specs import main

BODY = "# Topic\n\n# 1. Purpose and Scope\n"


def spec(status: str = "implemented", extra: str = "", **dates: str) -> str:
    created = dates.get("created", "2026-10-01")
    updated = dates.get("updated", "2026-10-02")
    return (
        f"---\nstatus: {status}\ncreated: {created}\nupdated: {updated}\n---\n\n"
        + BODY
        + extra
    )


def run(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> str:
    (tmp_path / "specs").mkdir(exist_ok=True)
    (tmp_path / "specs" / "topic.spec.md").write_text(text)
    main(["--write-index"], tmp_path)
    capsys.readouterr()
    code = main([], tmp_path)
    out = capsys.readouterr().out
    return "" if code == 0 else out


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (spec(), ""),
        (BODY, "missing YAML frontmatter"),
        (spec("done"), "status 'done' not one of"),
        (spec(created="2026-13-01"), "created must be YYYY-MM-DD"),
        (spec(updated="2999-01-01"), "is in the future"),
        (spec(created="2026-10-05"), "is before created"),
        (spec().replace("updated: 2026-10-02\n", ""), "frontmatter missing 'updated'"),
        (spec("partial"), "needs an 'Open items' section"),
        (spec("partial", "\n## Open items\n\n- 3.2\n"), ""),
        (spec("superseded"), "must link its replacement"),
        (spec("superseded", "\nSuperseded by [gone](gone.spec.md).\n"), "must link"),
        (spec("superseded", "\nSuperseded by [new](topic.spec.md).\n"), ""),
        (spec("superseded", "\nSuperseded by [a](a.md), [b](topic.spec.md).\n"), ""),
    ],
)
def test_spec_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, error: str
) -> None:
    out = run(tmp_path, text, capsys)
    assert (error in out) if error else out == ""


def test_index_must_list_every_spec_with_its_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "topic.spec.md").write_text(spec())
    assert main([], tmp_path) == 1
    assert "specs/README.md: index missing" in capsys.readouterr().out
    assert main(["--write-index"], tmp_path) == 0
    assert main([], tmp_path) == 0
    (specs / "topic.spec.md").write_text(spec("draft"))
    (specs / "other.spec.md").write_text(spec())
    assert main([], tmp_path) == 1
    out = capsys.readouterr().out
    assert "listed as 'implemented', frontmatter says 'draft'" in out
    assert "other.spec.md not listed in the index" in out
    (specs / "other.spec.md").unlink()
    (specs / "topic.spec.md").unlink()
    assert main([], tmp_path) == 1
    assert "lists topic.spec.md, which does not exist" in capsys.readouterr().out


def test_drafts_are_checked_and_indexed_on_their_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    specs, drafts = tmp_path / "specs", tmp_path / "drafts"
    specs.mkdir()
    drafts.mkdir()
    (specs / "topic.spec.md").write_text(spec())
    (drafts / "2026-10-01.idea.draft.md").write_text(
        spec("superseded", "\nSuperseded by [topic](../specs/topic.spec.md).\n")
    )
    assert main(["--write-index"], tmp_path) == 0
    index = (drafts / "README.md").read_text()
    assert "2026-10-01.idea.draft.md" in index
    assert "topic.spec.md" not in index
    (drafts / "2026-10-02.other.draft.md").write_text(spec("partial"))
    assert main([], tmp_path) == 1
    out = capsys.readouterr().out
    assert "drafts/2026-10-02.other.draft.md: partial doc needs" in out
    assert "drafts/README.md: 2026-10-02.other.draft.md not listed" in out


def test_repository_specs_pass() -> None:
    assert main([]) == 0
