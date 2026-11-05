"""The tool token budget: approximation, ordering and totals, on a fake tool list."""

from tool_tokens import build_report, count_tokens, format_report

from evals.model import ToolSpec


def fake_tools() -> list[ToolSpec]:
    return [
        ToolSpec("a_small", "Does x.", {"type": "object"}),
        ToolSpec(
            "b_big",
            "Lists every task with its status and owner, newest first.",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        ToolSpec("c_small", "Does x.", {"type": "object"}),
    ]


def test_count_is_deterministic_and_splits_words_and_punctuation() -> None:
    text = 'Send {"id": 12345} now, internationalization!'
    assert count_tokens(text) == count_tokens(text)
    assert count_tokens("") == 0
    assert count_tokens("abcd") == 1
    assert count_tokens("abcde") == 2
    assert count_tokens("a, b") == 3


def test_breakdown_is_sorted_and_totals_add_up() -> None:
    report = build_report("worker", fake_tools(), "Be brief.")
    names = [name for name, _ in report["tools"]]
    sizes = [n for _, n in report["tools"]]
    assert names == ["b_big", "a_small", "c_small"]  # largest first, ties by name
    assert sizes == sorted(sizes, reverse=True)
    assert report["prompt"] == count_tokens("Be brief.")
    assert report["total"] == report["prompt"] + sum(sizes)
    assert build_report("worker", fake_tools(), "Be brief.") == report
    assert format_report(report).startswith(f"worker: {report['total']} tokens")
