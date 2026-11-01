import pytest
from check_tests import check_source, main


def rules(source: str, kind: str = "test") -> list[tuple[int, str]]:
    return [(h.line, h.rule) for h in check_source(source, kind)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import time\ntime.sleep(1)\n", [(2, "sleep")]),
        (
            "import asyncio\nasync def t():\n    await asyncio.sleep(0.1)\n",
            [(3, "sleep")],
        ),
        ("import asyncio\nasync def t():\n    await asyncio.sleep(0)\n", []),
        (
            "import time\nwhile not ready():\n    time.sleep(1)\n",
            [(2, "poll-loop"), (3, "sleep")],
        ),
        (
            "import time\ndef t():\n    start = time.monotonic()\n"
            "    assert time.monotonic() - start < 1\n",
            [(4, "elapsed-time")],
        ),
        (
            "from datetime import datetime\nx = datetime.now() - y\n",
            [(2, "elapsed-time")],
        ),
        ("import time\nx = time.time()\n", []),
        (
            "import pytest\n@pytest.mark.timeout(5)\ndef t(): ...\n"
            "@pytest.mark.flaky\ndef u(): ...\n",
            [(2, "timeout-retry"), (4, "timeout-retry")],
        ),
        (
            "import pytest\n@pytest.mark.skip\ndef a(): ...\n"
            "@pytest.mark.skipif(x)\ndef b(): ...\n"
            "@pytest.mark.xfail(reason='bug')\ndef c(): ...\n"
            "@pytest.mark.skipif(x, reason='no docker')\ndef d(): ...\n",
            [(2, "skip-reason"), (4, "skip-reason")],
        ),
        (
            "a = 1  # noqa\nb = 2  # noqa: S101\nc = 3  # noqa: S101 fixture needs it\n"
            "d = 4  # type: ignore[x]\ne = 5  # type: ignore[x] stub is untyped\n"
            "# biome-ignore lint/a: \n",
            [
                (1, "suppress-reason"),
                (2, "suppress-reason"),
                (4, "suppress-reason"),
                (6, "suppress-reason"),
            ],
        ),
    ],
)
def test_test_rules(source: str, expected: list[tuple[int, str]]) -> None:
    assert rules(source) == expected


def test_allow_comment_needs_reason_and_matching_rule() -> None:
    ok = "import time\n# check-tests: allow sleep — fake CLI must outlive the poll\ntime.sleep(1)\n"
    no_reason = "import time\n# check-tests: allow sleep\ntime.sleep(1)\n"
    other_rule = "import time\n# check-tests: allow poll-loop — why\ntime.sleep(1)\n"
    not_adjacent = "import time\n# check-tests: allow sleep — why\n\ntime.sleep(1)\n"
    assert rules(ok) == []
    assert rules(no_reason) == [(3, "sleep")]
    assert rules(other_rule) == [(3, "sleep")]
    assert rules(not_adjacent) == [(4, "sleep")]


def test_string_literals_are_not_scanned() -> None:
    source = (
        'SCRIPT = """\nimport time\ntime.sleep(5)  # noqa\n"""\nX = "time.sleep(1)"\n'
    )
    assert rules(source) == []


def test_tx_rule_only_in_app_code_functions_receiving_tx() -> None:
    source = (
        "from datetime import datetime\nimport time\n"
        "async def a(tx: Tx):\n    return datetime.now()\n"
        "def b(tx: db.Tx):\n    return time.time()\n"
        "def c():\n    return datetime.now()\n"
    )
    assert rules(source, "app") == [(4, "tx-clock"), (6, "tx-clock")]
    assert rules(source, "test") == []


def test_report_only_by_default_strict_fails(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("check_tests.ROOT", tmp_path)
    (tmp_path / "test_x.py").write_text("import time\ntime.sleep(1)\n")
    assert main(["test_x.py"]) == 0
    assert "test_x.py:2: sleep" in capsys.readouterr().out
    assert main(["test_x.py", "--strict"]) == 1
