"""The task-side helper, without PostgreSQL or Valkey: its contract, its
isolation rules and what it refuses. The real servers are exercised by the
Docker smoke (apps/api/tests/test_runtime_docker.py)."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import argus_core
import pytest

SCRIPT = Path(argus_core.__file__).parent / "runtime_assets" / "argus-test-services"


@pytest.fixture
def ts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGUS_TEST_SERVICES_ROOT", str(tmp_path / "root"))
    loader = importlib.machinery.SourceFileLoader("argus_test_services", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_declaration_is_validated(ts) -> None:
    ok = ts.check_spec(
        {
            "task_id": "t-1",
            "services": [
                {
                    "kind": "postgres",
                    "env": {"RADAR_TEST_DATABASE_URL": "{url_asyncpg}"},
                }
            ],
        }
    )
    assert ok["task_id"] == "t-1"
    for bad in (
        {"task_id": "../x", "services": []},
        {"task_id": "t", "services": [{"kind": "mysql"}]},
        {"task_id": "t", "services": [{"kind": "valkey"}, {"kind": "valkey"}]},
        {"task_id": "t", "services": [{"kind": "valkey", "env": {"bad": "{url}"}}]},
        {
            "task_id": "t",
            "services": [{"kind": "valkey", "env": {"ABC_URL": "{database}"}}],
        },
        {"services": []},
    ):
        with pytest.raises(ts.Fail):
            ts.check_spec(bad)


def test_missing_declaration_is_an_explicit_error(ts, tmp_path: Path) -> None:
    with pytest.raises(ts.Fail, match="this task has none"):
        ts.load_spec(str(tmp_path / "absent.json"))


def test_templates_render_synthetic_values_only_for_the_task_instance(ts) -> None:
    state = {"kind": "postgres", "host": "127.0.0.1", "port": 5999, "user": "argus_test",
             "password": "pw", "database": "t_abc"}  # fmt: skip
    assert (
        ts.render(state, "{url_asyncpg}")
        == "postgresql+asyncpg://argus_test:pw@127.0.0.1:5999/t_abc"
    )
    assert ts.render(state, "{host}:{port}") == "127.0.0.1:5999"
    valkey = {"kind": "valkey", "host": "127.0.0.1", "port": 6001, "password": "pw"}
    assert ts.render(valkey, "{url}") == "redis://default:pw@127.0.0.1:6001/0"


def test_default_variable_names_when_none_are_mapped(ts) -> None:
    spec = {"task_id": "t", "services": [{"kind": "valkey"}]}
    states = {
        "valkey": {"kind": "valkey", "host": "127.0.0.1", "port": 1, "password": "p"}
    }
    assert ts.environment(spec, states) == {
        "ARGUS_TEST_VALKEY_URL": "redis://default:p@127.0.0.1:1/0"
    }


def test_down_removes_only_its_own_task_directory(ts) -> None:
    mine, other = ts.task_dir("task-a"), ts.task_dir("task-b")
    for d in (mine, other):
        d.mkdir(parents=True)
        (d / "state.json").write_text("{}")
        (d / "data").write_text("x")
    ts.down("task-a")
    assert not mine.exists() and (other / "data").exists()


def test_it_never_signals_a_process_it_cannot_prove_is_its_own(
    ts, tmp_path: Path
) -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        d = ts.task_dir("task-c")
        d.mkdir(parents=True)
        bogus = {
            "kind": "valkey",
            "pid": sleeper.pid,
            "marker": str(d / "valkey"),
            "host": "h",
            "port": 1,
            "password": "p",
        }
        (d / "state.json").write_text(json.dumps({"valkey": bogus}))
        ts.down("task-c")  # state names a live pid whose command line is not ours
        assert sleeper.poll() is None
        assert not d.exists()
    finally:
        sleeper.kill()


def test_task_ids_cannot_escape_the_root(ts) -> None:
    for bad in ("../etc", "a/b", "", "x" * 65):
        with pytest.raises(ts.Fail):
            ts.task_dir(bad)


def test_cli_reports_a_clear_error_without_a_declaration(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--spec", str(tmp_path / "none.json"), "up"],
        capture_output=True, text=True, env={**os.environ, "ARGUS_TEST_SERVICES_ROOT": str(tmp_path)},
    )  # fmt: skip
    assert done.returncode == 2 and "this task has none" in done.stderr
