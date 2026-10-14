from argus_api.main import app
from fastapi.testclient import TestClient


def test_healthz() -> None:
    res = TestClient(app).get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
