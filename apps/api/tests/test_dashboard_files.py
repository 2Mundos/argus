from pathlib import Path

from argus_api.main import DashboardFiles
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_html_revalidates_and_hashed_assets_cache_forever(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "assets" / "index-abc123.js").write_text("// js")
    app = FastAPI()
    app.mount("/", DashboardFiles(directory=tmp_path, html=True))
    client = TestClient(app)
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/assets/index-abc123.js").headers["cache-control"]
