"""Model observability contracts (docs/specs/model-observability.spec.md)."""

import pytest
from argus_core.model_trace import (
    MAX_EXPLANATION,
    ExecutionReport,
    ModelTraceReport,
    SelectionReport,
    clean_text,
)
from pydantic import ValidationError

RUN = {"key": "d1", "layer": "delegate", "state": "running"}
SEL = {"key": "s1", "layer": "harness", "reason_code": "quota"}


def test_clean_text_redacts_and_bounds() -> None:
    out = clean_text("Authorization: Bearer abcdefgh12345678 password=x ok")
    assert out is not None and "abcdefgh" not in out and "ok" in out
    assert clean_text("  \n ") is None and clean_text(None) is None
    long = clean_text("word " * 500)
    assert long is not None and len(long) == MAX_EXPLANATION
    assert "[redacted]" in (clean_text("A" * 40) or "")


def test_missing_metadata_is_explicitly_not_reported() -> None:
    run = ExecutionReport(**RUN)
    assert run.observed_model is None and run.evidence == "not_reported"


@pytest.mark.parametrize(
    "extra",
    [
        {"observed_model": "m"},  # observed without evidence
        {"evidence": "cli_output"},  # evidence without observed
        {"layer": "harness", "executor": "claude"},
        {"engine": "codex"},  # engine on the delegate layer
        {"requested_model": "sk-" + "a" * 30},
        {"key": "bad key"},
        {"resumed_from": "d1"},
        {"policy_revision": "NOTHEX"},
    ],
)
def test_execution_rejects_inconsistent_reports(extra: dict) -> None:
    with pytest.raises(ValidationError):
        ExecutionReport(**{**RUN, **extra})


@pytest.mark.parametrize(
    "kind, extra",
    [
        ("initial", {"from_model": "m"}),
        ("model_switch", {"to_model": "m"}),
        ("model_switch", {"from_model": "m", "to_model": "m"}),
        ("account_switch", {"from_account_id": "a", "to_account_id": "a"}),
        (
            "account_switch",
            {
                "from_account_id": "a",
                "to_account_id": "b",
                "from_model": "m",
                "to_model": "n",
            },
        ),
        ("provider_switch", {"from_provider": "x"}),
        ("executor_switch", {"from_executor": "claude", "to_executor": "codex"}),
    ],
)
def test_selection_kind_must_match_what_changed(kind: str, extra: dict) -> None:
    with pytest.raises(ValidationError):
        SelectionReport(**{**SEL, "kind": kind, **extra})


def test_valid_account_switch_and_explanation_cleaned() -> None:
    sel = SelectionReport(
        **SEL,
        kind="account_switch",
        from_account_id="a",
        to_account_id="b",
        from_model="m",
        to_model="m",
        explanation="token=abc123 hit quota",
    )
    assert "abc123" not in (sel.explanation or "")


def test_report_needs_content_and_is_bounded() -> None:
    with pytest.raises(ValidationError):
        ModelTraceReport(attempt=1)
    with pytest.raises(ValidationError):
        ModelTraceReport(attempt=1, executions=[RUN] * 21)
