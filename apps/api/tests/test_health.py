from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient


def test_healthz_runs_lifespan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    assert (tmp_path / "argus.db").exists()
