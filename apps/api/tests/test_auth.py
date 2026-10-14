import stat
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api import auth
from argus_api.db import Database
from argus_api.errors import ConflictError, UnauthorizedError
from argus_api.main import app
from argus_core.auth import Scope
from fastapi.testclient import TestClient

PRESET = "preset-admin-token-" + "y" * 32


async def ensure(db: Database, data_dir: Path, preset: str | None = None):
    async with db.write() as tx:
        return await auth.ensure_admin_token(tx, data_dir, preset)


async def whoami(db: Database, token: str) -> auth.Identity:
    async with db.read() as conn:
        return await auth.authenticate(conn, token)


# -- admin token bootstrap ----------------------------------------------------------


async def test_first_start_generates_admin_token_file(
    db: Database, tmp_path: Path
) -> None:
    token = await ensure(db, tmp_path)
    assert token is not None
    assert token.startswith("argus_admin_")
    path = tmp_path / "admin-token"
    assert path.read_text().strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (await whoami(db, token)).scope is Scope.ADMIN
    assert await ensure(db, tmp_path) is None  # idempotent


async def test_preset_admin_token(db: Database, tmp_path: Path) -> None:
    assert await ensure(db, tmp_path, PRESET) is None
    assert not (tmp_path / "admin-token").exists()
    identity = await whoami(db, PRESET)
    assert identity.scope is Scope.ADMIN
    assert identity.actor == "admin:preset"


async def test_short_preset_is_refused(db: Database, tmp_path: Path) -> None:
    with pytest.raises(auth.AdminTokenConfigError):
        await ensure(db, tmp_path, "short")


async def test_revoked_preset_stays_revoked(db: Database, tmp_path: Path) -> None:
    await ensure(db, tmp_path, PRESET)
    async with db.write() as tx:
        await auth.create_admin_token(tx, "backup", "admin:preset")
    preset_id = (await whoami(db, PRESET)).token_id
    async with db.write() as tx:
        await auth.revoke_admin_token(tx, preset_id)
    await ensure(db, tmp_path, PRESET)  # restart with the same env var
    with pytest.raises(UnauthorizedError):
        await whoami(db, PRESET)


async def test_last_admin_token_cannot_be_revoked(db: Database, tmp_path: Path) -> None:
    token = await ensure(db, tmp_path)
    assert token is not None
    token_id = (await whoami(db, token)).token_id
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await auth.revoke_admin_token(tx, token_id)
    assert err.value.code == "last_admin_token"


# -- scopes over HTTP -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", PRESET)
    with TestClient(app) as c:
        yield c


ADMIN = {"Authorization": f"Bearer {PRESET}"}


def agent(client: TestClient, role: str) -> dict[str, str]:
    boot = client.post(
        "/api/agents/bootstrap-tokens", json={"role": role}, headers=ADMIN
    ).json()["token"]
    token = client.post("/api/agents/register", json={"bootstrap_token": boot}).json()[
        "token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_scope_table(client: TestClient) -> None:
    worker, orch = agent(client, "worker"), agent(client, "orchestrator")
    task_id = client.post("/api/tasks", json={"title": "t"}, headers=ADMIN).json()["id"]
    worker_id = client.get("/api/agents?state=active", headers=ADMIN).json()[-1]["id"]

    def status(method: str, path: str, headers: dict[str, str], **kw) -> int:
        return client.request(method, path, headers=headers, **kw).status_code

    # Anonymous: only health and registration.
    assert status("GET", "/healthz", {}) == 200
    assert status("GET", "/api/tasks", {}) == 401
    assert (
        status("POST", "/api/agents/register", {}, json={"bootstrap_token": "x"}) == 401
    )

    # Reads: any token.
    for who in (worker, orch, ADMIN):
        assert status("GET", "/api/tasks", who) == 200
        assert status("GET", "/api/agents", who) == 200

    # Task management: orchestrator and admin, not workers.
    assert status("POST", "/api/tasks", worker, json={"title": "x"}) == 403
    assert status("POST", "/api/tasks", orch, json={"title": "x"}) == 201
    assert status("POST", f"/api/tasks/{task_id}/cancel", worker) == 403
    assert status("POST", f"/api/tasks/{task_id}/cancel", orch) == 200
    assert status("POST", f"/api/agents/{worker_id}/drain", worker) == 403
    assert status("POST", f"/api/agents/{worker_id}/drain", orch) == 200

    # Credentials: admin only.
    for who in (worker, orch):
        assert status("POST", "/api/agents/bootstrap-tokens", who, json={}) == 403
        assert status("GET", "/api/admin/tokens", who) == 403
    assert status("POST", "/api/agents/bootstrap-tokens", ADMIN, json={}) == 201

    # Worker operations: agents only (orchestrators are agents too).
    assert status("POST", "/api/tasks/claim", ADMIN, json={}) == 403
    assert status("POST", "/api/tasks/claim", orch, json={}) in (200, 204)


def test_admin_token_management(client: TestClient) -> None:
    issued = client.post(
        "/api/admin/tokens", json={"label": "ops"}, headers=ADMIN
    ).json()
    second = {"Authorization": f"Bearer {issued['token']}"}
    assert client.get("/api/admin/tokens", headers=second).status_code == 200
    labels = [t["label"] for t in client.get("/api/admin/tokens", headers=ADMIN).json()]
    assert labels == ["preset", "ops"]
    res = client.post(f"/api/admin/tokens/{issued['id']}/revoke", headers=ADMIN)
    assert res.status_code == 204
    assert client.get("/api/admin/tokens", headers=second).status_code == 401


def test_first_start_without_preset_logs_and_writes_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ARGUS_ADMIN_TOKEN", raising=False)
    with TestClient(app) as c:
        token = (tmp_path / "admin-token").read_text().strip()
        headers = {"Authorization": f"Bearer {token}"}
        assert c.get("/api/tasks", headers=headers).status_code == 200
    assert token in caplog.text
