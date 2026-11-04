"""Response compression keeps ranged and binary bodies byte-exact."""

import pytest
from argus_api.compression import Compression
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

BODY = [{"title": "task", "state": "ready"}] * 200


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/json")
    def json() -> JSONResponse:
        return JSONResponse(BODY)

    @app.get("/small")
    def small() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/file")
    def file() -> Response:
        return Response(b"\0" * 5000, media_type="application/octet-stream")

    app.add_middleware(Compression)
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "headers", "encoded"),
    [
        ("/json", {"Accept-Encoding": "gzip"}, True),
        ("/json", {"Accept-Encoding": "identity"}, False),
        ("/json", {"Accept-Encoding": "gzip", "Range": "bytes=0-99"}, False),
        ("/small", {"Accept-Encoding": "gzip"}, False),
        ("/file", {"Accept-Encoding": "gzip"}, False),
    ],
)
def test_compression(
    client: TestClient, path: str, headers: dict[str, str], encoded: bool
) -> None:
    res = client.get(path, headers=headers)
    assert res.status_code == 200
    assert (res.headers.get("content-encoding") == "gzip") is encoded
    if path == "/json" and "Range" not in headers:
        assert res.json() == BODY  # decoded transparently, and Vary tells caches why
        assert "Accept-Encoding" in res.headers["vary"]
