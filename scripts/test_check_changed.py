import subprocess
import sys
from pathlib import Path

import check_changed as cc
import pytest

API = "apps/api/src/argus_api/"
FILES = {
    f"{API}__init__.py": "",
    f"{API}core.py": "X = 1\n",
    f"{API}mid.py": "from argus_api import core\n",
    f"{API}sub/__init__.py": "",
    f"{API}sub/leaf.py": "from ..mid import X\n",
    f"{API}routes.py": "from fastapi import APIRouter\nrouter = APIRouter()\n",
    "apps/api/tests/test_leaf.py": "from argus_api.sub.leaf import X\n",
    "apps/api/tests/test_other.py": "def test_a() -> None: ...\n",
    "apps/dashboard/src/a.ts": "export const a = 1\n",
    "apps/dashboard/src/b.ts": "import { a } from './a'\n",
    "apps/dashboard/src/b.test.ts": "import { a } from './b.js'\n",
    "apps/dashboard/src/c.test.ts": "import { x } from '@argus/api-client'\n",
    "packages/api-client/src/index.ts": "export const x = 1\n",
    "scripts/check_changed.py": "",
}


@pytest.fixture(autouse=True)
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Materialize FILES in a temp root so existence checks and file reads see them."""
    for name, text in FILES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    monkeypatch.setattr(cc, "ROOT", tmp_path)
    return tmp_path


def steps_for(changed: list[str], cpus: int = 8) -> list[str]:
    return [s.name for s in cc.plan(changed, sorted(FILES), cc.Graph(FILES), cpus)]


def test_import_graph_follows_absolute_relative_and_package_imports() -> None:
    graph = cc.Graph(FILES)
    assert graph.dependents([f"{API}core.py"]) >= {
        f"{API}mid.py",
        f"{API}sub/leaf.py",
        "apps/api/tests/test_leaf.py",
    }
    assert "apps/api/tests/test_other.py" not in graph.dependents([f"{API}core.py"])
    # importing argus_api.mid runs argus_api/__init__.py, so its importers are affected too
    assert f"{API}mid.py" in cc.Graph(FILES).dependents([f"{API}__init__.py"])


def test_ts_graph_resolves_specifiers_and_workspace_alias() -> None:
    graph = cc.Graph(FILES)
    assert graph.dependents(["apps/dashboard/src/a.ts"]) >= {
        "apps/dashboard/src/b.ts",
        "apps/dashboard/src/b.test.ts",
    }
    assert "apps/dashboard/src/c.test.ts" in graph.dependents(
        ["packages/api-client/src/index.ts"]
    )


def test_docs_only_runs_only_docs_validation() -> None:
    assert steps_for(["docs/README.md"]) == ["validate docs"]


def test_dashboard_only_never_runs_pytest_or_python_tools() -> None:
    steps = cc.plan(["apps/dashboard/src/a.ts"], sorted(FILES), cc.Graph(FILES), 8)
    assert [s.name for s in steps] == ["biome", "tsc", "vitest", "dashboard build"]
    vitest = steps[2]
    assert "src/b.test.ts" in vitest.cmd and "src/c.test.ts" not in vitest.cmd


def test_api_client_change_runs_openapi_and_dashboard_tests_not_pytest() -> None:
    names = steps_for(["packages/api-client/openapi.json"])
    assert "openapi check" in names and "tsc" in names
    assert not any(n.startswith("pytest") for n in names)


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pnpm-lock.yaml",
        "apps/api/pyproject.toml",
        "apps/dashboard/package.json",
    ],
)
def test_lock_and_manifest_changes_run_full_check_and_build(path: str) -> None:
    assert steps_for([path, "docs/README.md"]) == ["make check", "make build"]


def test_migration_runs_preflight_and_full_suite() -> None:
    names = steps_for([f"{API}db/migrations/0066_x.sql"])
    assert names == ["pytest (full suite)", "migration preflight"]


def test_python_source_runs_tests_importing_it_plus_lint_ty_openapi() -> None:
    changed = [f"{API}routes.py", f"{API}sub/leaf.py"]
    steps = {s.name: s for s in cc.plan(changed, sorted(FILES), cc.Graph(FILES), 8)}
    assert steps.keys() == {
        "pytest (1 files)",
        "ruff check",
        "ruff format",
        "ty",
        "openapi check",
    }
    pytest_cmd = steps["pytest (1 files)"].cmd
    assert "apps/api/tests/test_leaf.py" in pytest_cmd
    assert "apps/api/tests/test_other.py" not in pytest_cmd


def test_test_beside_changed_module_runs_without_importing_it() -> None:
    steps = cc.plan(
        ["apps/api/src/argus_api/other.py"], sorted(FILES), cc.Graph(FILES), 8
    )
    assert "apps/api/tests/test_other.py" in steps[0].cmd


def test_scripts_python_is_linted_but_has_no_ty_or_openapi_step() -> None:
    assert steps_for(["scripts/check_changed.py"]) == ["ruff check", "ruff format"]


def test_conftest_change_runs_whole_suite() -> None:
    files = {**FILES, "apps/api/tests/conftest.py": ""}
    names = [
        s.name
        for s in cc.plan(
            ["apps/api/tests/conftest.py"], sorted(files), cc.Graph(files), 8
        )
    ]
    assert "pytest (full suite)" in names


def test_merge_base_missing_in_repo_without_origin_main(
    tmp_path: Path, monkeypatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    assert cc.merge_base() is None


def test_main_falls_back_to_full_check_without_merge_base(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cc, "merge_base", lambda ref="origin/main": None)
    assert cc.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert (
        "running the full check" in out and "make check" in out and "make build" in out
    )


def test_rerun_on_same_files_after_pass_skips_steps(
    monkeypatch, tmp_path, capsys
) -> None:
    step = cc.Step("validate docs", ("true",), 5)
    ran: list[list[cc.Step]] = []

    def fake_run(steps: list[cc.Step], budget: int) -> list[cc.Result]:
        ran.append(steps)
        return [cc.Result(s, "ok", 0.0) for s in steps]

    monkeypatch.setattr(cc, "merge_base", lambda ref="origin/main": "b" * 40)
    monkeypatch.setattr(cc, "changed_files", lambda base: ["docs/README.md"])
    monkeypatch.setattr(cc, "fingerprint", lambda base, changed: "k1")
    monkeypatch.setattr(cc, "read_graph_files", dict)
    monkeypatch.setattr(cc, "plan", lambda *a: [step])
    monkeypatch.setattr(cc, "passed_file", lambda: tmp_path / "passed")
    monkeypatch.setattr(cc, "run_steps", fake_run)
    assert cc.main([]) == 0 and len(ran) == 1
    capsys.readouterr()
    assert cc.main([]) == 0 and len(ran) == 1
    assert "already passed: validate docs" in capsys.readouterr().out
    monkeypatch.setattr(cc, "fingerprint", lambda base, changed: "k2")
    assert cc.main([]) == 0 and len(ran) == 2


def test_failed_run_is_not_recorded(monkeypatch, tmp_path) -> None:
    step = cc.Step("s", ("false",), 5)
    monkeypatch.setattr(cc, "merge_base", lambda ref="origin/main": "b" * 40)
    monkeypatch.setattr(cc, "changed_files", lambda base: ["docs/README.md"])
    monkeypatch.setattr(cc, "fingerprint", lambda base, changed: "k1")
    monkeypatch.setattr(cc, "read_graph_files", dict)
    monkeypatch.setattr(cc, "plan", lambda *a: [step])
    monkeypatch.setattr(cc, "passed_file", lambda: tmp_path / "passed")
    assert cc.main([]) == 1
    assert not (tmp_path / "passed").exists()


def test_step_timeout_kills_and_reports_failure() -> None:
    hang = cc.Step("hang", (sys.executable, "-c", "import time; time.sleep(600)"), 0.5)
    [res] = cc.run_steps([hang], 2)
    assert res.status == "TIMEOUT" and "killed after" in res.output


def test_run_steps_reports_exit_status_and_output(capsys) -> None:
    ok = cc.Step("ok", (sys.executable, "-c", "print('hi')"), 30)
    bad = cc.Step("bad", (sys.executable, "-c", "raise SystemExit(3)"), 30)
    results = {r.step.name: r.status for r in cc.run_steps([ok, bad], 1)}
    assert results == {"ok": "ok", "bad": "FAILED"}
    assert "=== ok: ok" in capsys.readouterr().out


def test_budget_comes_from_cpu_count_and_can_be_overridden(monkeypatch) -> None:
    monkeypatch.delenv("ARGUS_CHECK_JOBS", raising=False)
    monkeypatch.setattr(cc.os, "cpu_count", lambda: 6)
    assert cc.budget_from_env() == 6
    monkeypatch.setenv("ARGUS_CHECK_JOBS", "2")
    assert cc.budget_from_env() == 2
