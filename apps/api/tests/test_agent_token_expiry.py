"""Agent token absolute expiry (security-hardening spec)."""

from collections.abc import Generator
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from argus_api.agents.tokens import hash_token
from argus_api.db import ts
from argus_api.main import app
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "e" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ARGUS_AGENT_TOKEN_TTL_S", "3600")
    with TestClient(app) as c:
        yield c


def _register(client: TestClient) -> tuple[str, dict[str, str]]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": "worker"}, headers=ADMIN
    ).json()["token"]
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot}).json()
    return reg["token"], {"Authorization": f"Bearer {reg['token']}"}


async def _set_expires(client: TestClient, token: str, delta: timedelta) -> str:
    async with client.app.state.db.write() as tx:
        expires = ts(tx.now + delta)
        await tx.conn.execute(
            "UPDATE tokens SET expires_at = ? WHERE token_hash = ?",
            (expires, hash_token(token)),
        )
        return expires


async def _read_expires(client: TestClient, token: str) -> str | None:
    async with (
        client.app.state.db.read() as conn,
        conn.execute(
            "SELECT expires_at FROM tokens WHERE token_hash = ?",
            (hash_token(token),),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return row["expires_at"]


def test_expired_agent_token_is_rejected(client: TestClient) -> None:
    token, headers = _register(client)
    assert client.get("/api/tasks", headers=headers).status_code == 200
    anyio.run(_set_expires, client, token, timedelta(seconds=-1))
    res = client.get("/api/tasks", headers=headers)
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "token_expired"


def test_heartbeat_extends_near_expiry(client: TestClient) -> None:
    token, headers = _register(client)
    before = anyio.run(_set_expires, client, token, timedelta(seconds=100))
    assert (
        client.post("/api/agents/me/heartbeat", json={}, headers=headers).status_code
        == 200
    )
    after = anyio.run(_read_expires, client, token)
    assert after is not None and after > before


def test_bootstrap_token_path_unaffected(client: TestClient) -> None:
    boot = client.post(
        "/api/agents/bootstrap-tokens",
        json={"role": "worker", "ttl_s": 60},
        headers=ADMIN,
    ).json()
    assert "expires_at" in boot
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    assert reg.status_code == 201
