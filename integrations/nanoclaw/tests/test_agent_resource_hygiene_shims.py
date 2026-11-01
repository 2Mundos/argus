"""Resource-hygiene shims (docs/specs/agent-resource-hygiene.spec.md 3.1, 3.2).

The shell shims run in a real bash with the score file and cgroup file pointed at
temp files, so no test needs a container or touches the real oom_score_adj. The
Node preload and the sitecustomize run in their real interpreters.
"""

import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NANOCLAW = Path(__file__).resolve().parents[1]
SHIM = NANOCLAW / "argus-oom-priority.sh"
LIB = "/opt/argus-lib"
REQUIRE = f"--require={LIB}/argus-cpu-count.cjs"


def _bash(
    tmp_path: Path,
    script: str,
    *,
    cpu_max: str | None = None,
    score: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": os.environ["PATH"],
        "ARGUS_LIB_DIR": LIB,
        "ARGUS_CPU_MAX_FILE": str(tmp_path / "cpu.max"),
        "ARGUS_OOM_SCORE_FILE": str(tmp_path / "oom_score_adj"),
        "BASH_ENV": str(SHIM),
    }
    if cpu_max is not None:
        (tmp_path / "cpu.max").write_text(cpu_max)
    if score is not None:
        (tmp_path / "oom_score_adj").write_text(score)
    return subprocess.run(
        ["bash", "-c", script],
        env=base | (env or {}),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _score(tmp_path: Path) -> str:
    return (tmp_path / "oom_score_adj").read_text().strip()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("0", "500"),
        ("-998", "500"),
        ("499", "500"),
        ("500", "500"),
        ("900", "900"),  # never lowers
        ("garbage", "garbage"),
        ("", ""),
    ],
)
def test_oom_score_is_raise_only(tmp_path: Path, before: str, after: str) -> None:
    result = _bash(tmp_path, "true", score=before)
    assert result.returncode == 0
    assert result.stdout == ""
    assert (tmp_path / "oom_score_adj").read_text().strip() == after


def test_oom_score_errors_never_fail_or_print(tmp_path: Path) -> None:
    # Missing file (read error) and a directory (write error) are silent no-ops.
    for target in (tmp_path / "missing", tmp_path):
        result = _bash(
            tmp_path,
            "echo ran",
            env={"ARGUS_OOM_SCORE_FILE": str(target)},
        )
        assert (result.returncode, result.stdout, result.stderr) == (0, "ran\n", "")


def test_sourcing_twice_changes_nothing_more(tmp_path: Path) -> None:
    script = f'. "{SHIM}"; a="$NODE_OPTIONS|$PYTHONPATH"; . "{SHIM}"; echo "$NODE_OPTIONS|$PYTHONPATH"; echo "$a"'
    result = _bash(tmp_path, script, cpu_max="200000 100000", score="0")
    first, second = result.stdout.splitlines()
    assert first == second
    assert first.count("argus-cpu-count.cjs") == 1
    assert result.stderr == ""
    assert _score(tmp_path) == "500"


@pytest.mark.parametrize(
    ("cpu_max", "limit", "expected"),
    [
        ("200000 100000", None, "2"),
        ("150000 100000", None, "2"),  # rounds up
        ("50000 100000", None, "1"),  # at least one
        ("200000 100000", "8", "2"),  # cpu.max wins over the fallback
        (None, "3", "3"),
        (None, "1.5", "2"),
        (None, "0.25", "1"),
    ],
)
def test_cpu_count_from_quota_or_fallback(
    tmp_path: Path, cpu_max: str | None, limit: str | None, expected: str
) -> None:
    env = {"CONTAINER_CPU_LIMIT": limit} if limit else {}
    result = _bash(
        tmp_path,
        'echo "$ARGUS_CPU_COUNT $OMP_NUM_THREADS $PYTHON_CPU_COUNT"',
        cpu_max=cpu_max,
        env=env,
    )
    assert result.stdout.split() == [expected] * 3
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("cpu_max", "limit"),
    [
        (None, None),
        ("max 100000", None),
        ("max 100000", "4"),  # a file that says "no quota" is not "missing"
        (None, "many"),
        ("garbage", None),
    ],
)
def test_no_quota_sets_nothing(
    tmp_path: Path, cpu_max: str | None, limit: str | None
) -> None:
    env = {"CONTAINER_CPU_LIMIT": limit} if limit else {}
    result = _bash(
        tmp_path,
        'echo "[$ARGUS_CPU_COUNT$OMP_NUM_THREADS$PYTHON_CPU_COUNT$NODE_OPTIONS$PYTHONPATH]"',
        cpu_max=cpu_max,
        env=env,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "[]\n", "")


def test_values_the_user_set_win(tmp_path: Path) -> None:
    result = _bash(
        tmp_path,
        'echo "$OMP_NUM_THREADS $PYTHON_CPU_COUNT $NODE_OPTIONS $PYTHONPATH"',
        cpu_max="200000 100000",
        env={
            "OMP_NUM_THREADS": "7",
            "PYTHON_CPU_COUNT": "default",
            "NODE_OPTIONS": "--max-old-space-size=512",
            "PYTHONPATH": "/proj",
        },
    )
    assert result.stdout.split() == [
        "7",
        "default",
        "--max-old-space-size=512",
        REQUIRE,
        f"{LIB}/pycpu:/proj",
    ]


def test_node_options_replaced_by_a_project_logs_one_line_and_runs(
    tmp_path: Path,
) -> None:
    # The first shell installs the preload; the nested one sees it replaced.
    result = _bash(
        tmp_path,
        "NODE_OPTIONS=--inspect bash -c 'echo \"$NODE_OPTIONS\"'",
        cpu_max="200000 100000",
    )
    assert result.stdout.strip() == f"--inspect {REQUIRE}"
    assert result.stderr.count("NODE_OPTIONS was replaced") == 1
    assert len(result.stderr.splitlines()) == 1


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("count", "expected"),
    [("2", "2"), ("1000", None), ("0", None), ("two", None), (None, None)],
)
def test_node_preload_trims_os_cpus(count: str | None, expected: str | None) -> None:
    assert NODE is not None
    env = {"PATH": os.environ["PATH"]} | ({"ARGUS_CPU_COUNT": count} if count else {})
    code = "console.log(require('node:os').cpus().length)"
    host = subprocess.run(
        [NODE, "-e", code], env=env, capture_output=True, text=True, timeout=30
    ).stdout.strip()
    result = subprocess.run(
        [NODE, "--require", str(NANOCLAW / "argus-cpu-count.cjs"), "-e", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stderr == ""
    # A count above the host's, zero or junk leaves the host value alone.
    assert result.stdout.strip() == (expected or host)


@pytest.mark.parametrize(
    ("version", "count", "overridden"),
    [
        ((3, 12, 0), "2", True),
        ((3, 12, 0), "default", False),
        ((3, 12, 0), "0", False),
        ((3, 12, 0), "-1", False),
        ((3, 12, 0), None, False),
        ((3, 13, 0), "2", False),  # 3.13+ honours PYTHON_CPU_COUNT itself
    ],
)
def test_sitecustomize_overrides_cpu_count_before_313(
    monkeypatch: pytest.MonkeyPatch,
    version: tuple[int, int, int],
    count: str | None,
    overridden: bool,
) -> None:
    # Run in-process with a faked version: the CI interpreter may be 3.13+.
    monkeypatch.setattr(sys, "version_info", version)
    monkeypatch.setattr(os, "cpu_count", host := lambda: 24)
    monkeypatch.delenv("PYTHON_CPU_COUNT", raising=False)
    if count:
        monkeypatch.setenv("PYTHON_CPU_COUNT", count)
    runpy.run_path(str(NANOCLAW / "argus-cpu-count-sitecustomize.py"))
    assert os.cpu_count() == (2 if overridden else host())
