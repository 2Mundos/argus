"""Response headers and API docs opt-in (security-hardening spec)."""

from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "s" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def test_healthz_sets_baseline_headers(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_api_docs_are_off_by_default(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    # Schema remains available in-process for export/tests.
    assert "paths" in app.openapi()
