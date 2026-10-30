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


def test_root_icons_and_manifest_are_served(tmp_path: Path) -> None:
    """Vite copies public/ (favicons, manifest) to the dist root; browsers request
    them at fixed root paths, so they must be served there with the right type."""
    expected = {
        "favicon.ico": "image/vnd.microsoft.icon",
        "favicon-32x32.png": "image/png",
        "apple-touch-icon.png": "image/png",
        "site.webmanifest": "application/manifest+json",
    }
    (tmp_path / "index.html").write_text("<html></html>")
    for name in expected:
        (tmp_path / name).write_bytes(b"x")
    app = FastAPI()
    app.mount("/", DashboardFiles(directory=tmp_path, html=True))
    client = TestClient(app)
    for name, content_type in expected.items():
        response = client.get(f"/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"].startswith(content_type), name
