"""Codex accounts: import validation and the dedicated login (codex accounts spec)."""

import asyncio
import contextlib
import json
import os
import signal
import stat
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.accounts.codex_login import (
    CodexLoginManager,
    LoginState,
    LoginUnavailableError,
    url_allowed,
)
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.engines import catalog as catalog_mod
from argus_api.engines import router as routing
from argus_api.errors import ConflictError, NotFoundError
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_core.engines import (
    AccountsSummary,
    Fact,
    RouteRequest,
)
from fastapi.testclient import TestClient

ADMIN_TOKEN = "t" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

SECRET_ACCESS = "access-token-SECRET-1"
SECRET_REFRESH = "refresh-token-SECRET-2"


def auth_doc(**over) -> dict:
    doc = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "id-token-SECRET-0",
            "access_token": SECRET_ACCESS,
            "refresh_token": SECRET_REFRESH,
            "account_id": "acct-1",
        },
        "last_refresh": "2026-01-01T00:00:00Z",
    }
    doc.update(over)
    return doc


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


# -- import -------------------------------------------------------------------------


def test_import_creates_a_subscription_account_without_echoing_tokens(
    client: TestClient,
) -> None:
    res = client.post(
        "/api/accounts",
        json={"name": "codex-desktop", "provider": "openai", "credential": auth_doc()},
        headers=ADMIN,
    )
    assert res.status_code == 201, res.text
    account = res.json()
    assert account["kind"] == "subscription"  # derived from the document
    listed = client.get("/api/accounts", headers=ADMIN).text
    one = client.get(f"/api/accounts/{account['id']}", headers=ADMIN).text
    events = client.get("/api/events?limit=50", headers=ADMIN).text
    for text in (res.text, listed, one, events):
        assert SECRET_ACCESS not in text and SECRET_REFRESH not in text
    assert app.state.secrets.get(f"account-{account['id']}")["tokens"]["refresh_token"]


def test_import_apikey_document_is_an_api_key_account(client: TestClient) -> None:
    doc = {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-key-SECRET"}
    res = client.post(
        "/api/accounts",
        json={
            "name": "codex-key",
            "provider": "openai",
            "kind": "subscription",  # wrong on purpose: the document decides
            "credential": doc,
        },
        headers=ADMIN,
    )
    assert res.status_code == 201
    assert res.json()["kind"] == "api_key"
    assert "sk-key-SECRET" not in res.text


@pytest.mark.parametrize(
    "doc,fragment",
    [
        ({}, "auth_mode"),
        ({"auth_mode": "other"}, "auth_mode"),
        ({"auth_mode": "chatgpt"}, "tokens"),
        (
            {"auth_mode": "chatgpt", "tokens": {"access_token": "LEAK-A"}},
            "refresh_token",
        ),
        ({"auth_mode": "apikey", "OPENAI_API_KEY": ""}, "OPENAI_API_KEY"),
    ],
)
def test_import_rejects_bad_shapes_naming_fields_only(
    client: TestClient, doc: dict, fragment: str
) -> None:
    res = client.post(
        "/api/accounts",
        json={"name": "bad", "provider": "openai", "credential": doc},
        headers=ADMIN,
    )
    assert res.status_code == 422
    assert fragment in res.text
    assert "LEAK" not in res.text
    assert client.get("/api/accounts", headers=ADMIN).json() == []


def test_import_is_admin_only(client: TestClient) -> None:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    worker = {"Authorization": f"Bearer {reg.json()['token']}"}
    res = client.post(
        "/api/accounts",
        json={"name": "x", "provider": "openai", "credential": auth_doc()},
        headers=worker,
    )
    assert res.status_code == 403


def test_credential_update_is_validated_for_openai(client: TestClient) -> None:
    res = client.post(
        "/api/accounts",
        json={"name": "c", "provider": "openai", "credential": auth_doc()},
        headers=ADMIN,
    )
    account_id = res.json()["id"]
    bad = client.put(
        f"/api/accounts/{account_id}/credential",
        json={"credential": {"auth_mode": "chatgpt"}},
        headers=ADMIN,
    )
    assert bad.status_code == 422
    good = client.put(
        f"/api/accounts/{account_id}/credential",
        json={"credential": auth_doc()},
        headers=ADMIN,
    )
    assert good.status_code == 204


# -- dedicated login ----------------------------------------------------------------

FAKE = """\
#!{python}
import json, os, sys, time
mode = {mode!r}
home = os.environ["CODEX_HOME"]
go = {go!r}
def out(m):
    sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        out({{"id": msg["id"], "result": {{}}}})
    elif msg.get("method") == "account/login/start":
        assert msg["params"] == {{"type": "chatgptDeviceCode"}}
        if mode in ("tree", "stubborn"):
            import subprocess
            code = "import signal,time;" + (
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);" if mode == "stubborn" else ""
            ) + "time.sleep(30)"
            child = subprocess.Popen([sys.executable, "-c", code])
            with open({pids!r}, "w") as f:
                f.write(str(child.pid))
        url = "https://evil.example/device" if mode == "badurl" else "https://auth.openai.com/codex/device"
        out({{"id": msg["id"], "result": {{"type": "chatgptDeviceCode", "loginId": "L1",
             "verificationUrl": url, "userCode": "ABCD-1234"}}}})
        if mode in ("hang", "tree", "stubborn"):
            time.sleep(60)
        while not os.path.exists(go):
            time.sleep(0.02)
        if mode == "reject":
            out({{"method": "account/login/completed", "params": {{"success": False, "error": "denied-SECRET"}}}})
        else:
            doc = {doc!r}
            with open(os.path.join(home, "auth.json"), "w") as f:
                json.dump(doc, f)
            out({{"method": "account/login/completed", "params": {{"success": True, "loginId": "L1"}}}})
        break
"""


def fake_codex(tmp_path: Path, mode: str = "ok") -> tuple[str, Path]:
    go = tmp_path / "go"
    script = tmp_path / f"codex-{mode}"
    script.write_text(
        FAKE.format(
            python=sys.executable,
            mode=mode,
            go=str(go),
            doc=auth_doc(),
            pids=str(tmp_path / "child.pid"),
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), go


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


def manager(db: Database, secrets, tmp_path: Path, mode: str = "ok", **kw):
    binary, go = fake_codex(tmp_path, mode)
    settings = Settings(data_dir=tmp_path, codex_bin=binary)
    return CodexLoginManager(db, secrets, settings, **kw), go


async def test_login_completes_into_an_account(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, go = manager(db, secrets, tmp_path)
    before = set(Path(tempfile_dir()).glob("argus-codex-login-*"))
    started = await mgr.start("tok-1", "admin:one", "Codex main")
    assert started["state"] == "pending"
    assert started["verification_url"] == "https://auth.openai.com/codex/device"
    assert started["user_code"] == "ABCD-1234"
    (home,) = set(Path(tempfile_dir()).glob("argus-codex-login-*")) - before
    assert oct(home.stat().st_mode & 0o777) == "0o700"

    go.write_text("")
    view = await settle(mgr, started["login_id"], "tok-1")
    assert view["state"] == "completed", view
    # The code is shown only while pending.
    assert view["user_code"] is None and view["verification_url"] is None
    async with db.read() as conn:
        async with conn.execute(
            "SELECT provider, kind, secret_ref FROM accounts WHERE id = ?",
            (view["account_id"],),
        ) as cur:
            row = await cur.fetchone()
        async with conn.execute("SELECT data FROM events") as cur:
            events = " ".join(r["data"] for r in await cur.fetchall())
    assert (row["provider"], row["kind"]) == ("openai", "subscription")
    stored = secrets.get(row["secret_ref"].removeprefix("sops:"))
    assert stored["tokens"]["refresh_token"] == SECRET_REFRESH
    assert "ABCD-1234" not in events and SECRET_REFRESH not in events
    assert not [
        h for h in Path(tempfile_dir()).glob("argus-codex-login-*") if h not in before
    ]


def tempfile_dir() -> str:
    return tempfile.gettempdir()


@pytest.fixture(autouse=True)
def own_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tempdir per test: parallel workers' logins share /tmp otherwise."""
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))


async def test_login_isolated_by_owner_and_limited(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path)
    a = await mgr.start("tok-a", "admin:a", None)
    with pytest.raises(ConflictError) as same:
        await mgr.start("tok-a", "admin:a", None)
    assert same.value.code == "login_in_progress"
    # Another token cannot read, learn the code of, or cancel it.
    with pytest.raises(NotFoundError):
        mgr.get(a["login_id"], "tok-b")
    with pytest.raises(NotFoundError):
        await mgr.cancel(a["login_id"], "tok-b")
    await mgr.start("tok-b", "admin:b", None)
    with pytest.raises(ConflictError) as limit:
        await mgr.start("tok-c", "admin:c", None)
    assert limit.value.code == "login_limit"
    await mgr.shutdown()


async def test_cancel_kills_the_client_and_removes_its_home(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="hang")
    started = await mgr.start("tok-1", "admin:one", None)
    session = mgr._sessions[started["login_id"]]
    home, proc = session.home, session.proc
    assert home is not None and home.exists() and proc is not None
    view = await mgr.cancel(started["login_id"], "tok-1")
    assert view["state"] == LoginState.CANCELLED
    assert view["user_code"] is None
    assert not home.exists()
    assert proc.returncode is not None
    # A new login can start afterwards, and a finished one stays readable.
    assert mgr.get(started["login_id"], "tok-1")["state"] == "cancelled"
    await mgr.start("tok-1", "admin:one", None)
    await mgr.shutdown()


async def test_login_expires(db: Database, secrets, tmp_path: Path) -> None:
    now = [0.0]
    mgr, _ = manager(db, secrets, tmp_path, mode="hang", clock=lambda: now[0])
    started = await mgr.start("tok-1", "admin:one", None)
    assert started["expires_in_s"] == 900
    now[0] = 901
    assert mgr.get(started["login_id"], "tok-1")["state"] == "expired"
    await mgr.shutdown()


async def test_login_refuses_a_foreign_verification_url(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="badurl")
    before = set(Path(tempfile_dir()).glob("argus-codex-login-*"))
    with pytest.raises(LoginUnavailableError) as err:
        await mgr.start("tok-1", "admin:one", None)
    assert "url_not_allowed" in str(err.value)
    assert set(Path(tempfile_dir()).glob("argus-codex-login-*")) == before


async def test_rejected_login_creates_no_account_and_hides_the_reason(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, go = manager(db, secrets, tmp_path, mode="reject")
    started = await mgr.start("tok-1", "admin:one", None)
    go.write_text("")
    view = await settle(mgr, started["login_id"], "tok-1")
    assert view["state"] == "failed" and view["error"] == "login_rejected"
    assert "SECRET" not in json.dumps(view)
    assert secrets.names() == set()


async def test_login_unavailable_without_a_client(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr = CodexLoginManager(db, secrets, Settings(data_dir=tmp_path))
    with pytest.raises(LoginUnavailableError):
        await mgr.start("tok", "admin:x", None)


# -- expiry, cancel and persistence are linearised ----------------------------------


class GatedStore(MemorySecretStore):
    """Holds `put` open so a test can act while the account is being persisted."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def put(self, name: str, value: dict) -> None:
        self.entered.set()
        await self.release.wait()
        await super().put(name, value)


async def account_count(db: Database) -> int:
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) AS n FROM accounts") as cur,
    ):
        return (await cur.fetchone())["n"]


async def settle(mgr: CodexLoginManager, login_id: str, owner: str) -> dict:
    """The login's outcome, once its run task (and the teardown it awaits) ended."""
    task = mgr._sessions[login_id].task
    assert task is not None
    await asyncio.wait({task})  # does not raise if the run was cancelled
    return mgr.get(login_id, owner)


async def test_late_completion_after_expiry_creates_nothing(
    db: Database, secrets, tmp_path: Path
) -> None:
    now = [0.0]
    mgr, go = manager(db, secrets, tmp_path, clock=lambda: now[0])
    started = await mgr.start("tok-1", "admin:one", None)
    session = mgr._sessions[started["login_id"]]
    home, proc = session.home, session.proc
    now[0] = 901
    assert mgr.get(started["login_id"], "tok-1")["state"] == "expired"
    go.write_text("")  # the client would now report success
    assert session.task is not None
    await asyncio.wait({session.task})  # expiry cancelled it after the teardown
    view = mgr.get(started["login_id"], "tok-1")
    assert view["state"] == "expired" and view["account_id"] is None
    assert await account_count(db) == 0 and secrets.names() == set()
    assert home is not None and not home.exists()
    assert proc is not None and proc.returncode is not None
    await mgr.shutdown()


async def test_completion_that_finds_the_login_expired_persists_nothing(
    db: Database, secrets, tmp_path: Path
) -> None:
    # Nobody polled `get`: the guard in `_complete` itself must see the clock.
    now = [0.0]
    mgr, go = manager(db, secrets, tmp_path, clock=lambda: now[0])
    started = await mgr.start("tok-1", "admin:one", None)
    now[0] = 901
    go.write_text("")
    view = await settle(mgr, started["login_id"], "tok-1")
    assert view["state"] == "expired" and view["account_id"] is None
    assert await account_count(db) == 0 and secrets.names() == set()
    await mgr.shutdown()


async def test_cancel_during_persistence_reports_the_completed_account(
    db: Database, tmp_path: Path
) -> None:
    store = GatedStore()
    mgr, go = manager(db, store, tmp_path)
    started = await mgr.start("tok-1", "admin:one", None)
    go.write_text("")
    await asyncio.wait_for(store.entered.wait(), 5)
    cancel = asyncio.create_task(mgr.cancel(started["login_id"], "tok-1"))
    await asyncio.sleep(0)  # let the cancel reach its wait on the commit
    assert not cancel.done()  # waits for the outcome instead of racing it
    store.release.set()
    view = await asyncio.wait_for(cancel, 5)
    assert view["state"] == "completed" and view["account_id"]
    assert await account_count(db) == 1
    assert len(store.names()) == 1


async def test_expiry_during_persistence_does_not_hide_the_account(
    db: Database, tmp_path: Path
) -> None:
    now = [0.0]
    store = GatedStore()
    mgr, go = manager(db, store, tmp_path, clock=lambda: now[0])
    started = await mgr.start("tok-1", "admin:one", None)
    go.write_text("")
    await asyncio.wait_for(store.entered.wait(), 5)
    now[0] = 901
    assert mgr.get(started["login_id"], "tok-1")["state"] == "pending"
    store.release.set()
    view = await settle(mgr, started["login_id"], "tok-1")
    assert view["state"] == "completed" and view["account_id"]
    assert await account_count(db) == 1


async def test_cancel_before_persistence_wins_and_creates_nothing(
    db: Database, tmp_path: Path
) -> None:
    store = GatedStore()
    mgr, go = manager(db, store, tmp_path)
    started = await mgr.start("tok-1", "admin:one", None)
    session = mgr._sessions[started["login_id"]]
    view = await mgr.cancel(started["login_id"], "tok-1")
    go.write_text("")
    assert session.task is not None and session.task.done()  # the client is gone
    assert view["state"] == "cancelled" and not store.entered.is_set()
    assert mgr.get(started["login_id"], "tok-1")["account_id"] is None
    assert await account_count(db) == 0


async def test_failed_persistence_leaves_no_secret(
    db: Database, secrets, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus_api.accounts import service

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "create_account", boom)
    mgr, go = manager(db, secrets, tmp_path)
    started = await mgr.start("tok-1", "admin:one", None)
    session = mgr._sessions[started["login_id"]]
    home = session.home
    go.write_text("")
    view = await settle(mgr, started["login_id"], "tok-1")
    assert view["state"] == "failed" and view["error"] == "RuntimeError"
    assert secrets.names() == set() and await account_count(db) == 0
    assert home is not None and not home.exists()


# -- the client's whole process group goes away -------------------------------------


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"  # a zombie is dead
    except (FileNotFoundError, ProcessLookupError):
        return False


async def _gone(pid: int, seconds: float = 8) -> bool:
    """Await the process exit: a pidfd turns readable then, child of ours or not."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    loop = asyncio.get_running_loop()
    exited = asyncio.Event()
    loop.add_reader(fd, exited.set)
    try:
        await asyncio.wait_for(exited.wait(), seconds)  # a failure guard only
    except TimeoutError:
        return False
    finally:
        loop.remove_reader(fd)
        os.close(fd)
    return True


@pytest.mark.parametrize("mode", ["tree", "stubborn"])
@pytest.mark.parametrize("how", ["cancel", "shutdown", "expiry"])
async def test_stopping_a_login_stops_the_clients_descendants(
    db: Database,
    secrets,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    how: str,
) -> None:
    from argus_api.accounts import login_base

    monkeypatch.setattr(login_base, "_TERM_GRACE_S", 0.3)
    now = [0.0]
    mgr, _ = manager(db, secrets, tmp_path, mode=mode, clock=lambda: now[0])
    started = await mgr.start("tok-1", "admin:one", None)
    child = int((tmp_path / "child.pid").read_text())
    try:
        assert _alive(child)  # the control: it really is running before
        if how == "cancel":
            await mgr.cancel(started["login_id"], "tok-1")
        elif how == "shutdown":
            await mgr.shutdown()
        else:
            now[0] = 901
            assert mgr.get(started["login_id"], "tok-1")["state"] == "expired"
            await mgr.shutdown()  # waits for the cleanup the expiry scheduled
        assert await _gone(child)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
        await mgr.shutdown()


async def test_docker_mode_removes_exactly_its_container(
    db: Database, secrets, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _ = fake_codex(tmp_path, "tree")
    log = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'run':\n"
        "    home = sys.argv[sys.argv.index('-v') + 1].split(':')[0]\n"
        f"    os.execve({codex!r}, [{codex!r}], {{'CODEX_HOME': home}})\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    mgr = CodexLoginManager(
        db, secrets, Settings(data_dir=tmp_path, codex_login_image="img:pinned")
    )
    started = await mgr.start("tok-1", "admin:one", None)
    child = int((tmp_path / "child.pid").read_text())
    name = f"argus-codex-login-{started['login_id'][:12]}"
    try:
        await mgr.cancel(started["login_id"], "tok-1")
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert ["rm", "-f", name] in calls and sum(c[0] == "rm" for c in calls) == 1
        run = next(c for c in calls if c[0] == "run")
        assert run[run.index("--name") + 1] == name
        assert await _gone(child)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
        await mgr.shutdown()


async def test_every_cleanup_caller_waits_for_the_one_container_removal(
    db: Database, secrets, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `docker rm` blocks on a gate and logs only when it finishes. Cancel, the run task's own
    # cleanup and a second caller all race on it: each must return only after the
    # removal completed, and exactly one `rm` may run.
    codex, _ = fake_codex(tmp_path, "tree")
    log = tmp_path / "docker.log"
    started_fifo, gate_fifo = tmp_path / "rm.started", tmp_path / "rm.gate"
    os.mkfifo(started_fifo)
    os.mkfifo(gate_fifo)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[1] == 'run':\n"
        f"    open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "    home = sys.argv[sys.argv.index('-v') + 1].split(':')[0]\n"
        f"    os.execve({codex!r}, [{codex!r}], {{'CODEX_HOME': home}})\n"
        f"open({str(started_fifo)!r}, 'w').write('1')\n"  # blocks for the test
        f"open({str(gate_fifo)!r}).read()\n"  # blocks until the test opens the gate
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    mgr = CodexLoginManager(
        db, secrets, Settings(data_dir=tmp_path, codex_login_image="img:pinned")
    )
    started = await mgr.start("tok-1", "admin:one", None)
    child = int((tmp_path / "child.pid").read_text())
    session = mgr._sessions[started["login_id"]]
    name = f"argus-codex-login-{started['login_id'][:12]}"

    def removals() -> list[list[str]]:
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return [c for c in calls if c[0] == "rm"]

    rm_running = False

    async def open_gate() -> None:
        await asyncio.to_thread(gate_fifo.write_text, "")

    try:
        first = asyncio.create_task(mgr.cancel(started["login_id"], "tok-1"))
        # The first teardown is now waiting on `docker rm`.
        await asyncio.to_thread(started_fifo.read_text)
        rm_running = True
        second = asyncio.create_task(mgr._kill(session))
        await asyncio.sleep(0)  # the late caller joins the teardown in flight
        assert not first.done() and not second.done()
        assert removals() == []
        await open_gate()
        rm_running = False
        await asyncio.wait_for(second, 10)
        assert removals() == [["rm", "-f", name]]  # the late caller waited for it
        await asyncio.wait_for(first, 10)
        assert removals() == [["rm", "-f", name]]
        assert await _gone(child)
        # A caller cancelled mid-removal does not abandon it.
        await mgr.shutdown()
        assert len(removals()) == 1
    finally:
        if rm_running:
            await open_gate()
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
        await mgr.shutdown()


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://auth.openai.com/codex/device", True),
        ("https://chatgpt.com/auth", True),
        ("http://auth.openai.com/x", False),
        ("https://auth.openai.com.evil.example/", False),
        ("https://evilopenai.com/", False),
        ("https://user:pw@auth.openai.com/", False),
        ("javascript:alert(1)", False),
        ("", False),
    ],
)
def test_url_allowlist(url: str, ok: bool) -> None:
    assert url_allowed(url) is ok


def test_login_routes_need_admin_and_report_unavailable(client: TestClient) -> None:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    worker = {"Authorization": f"Bearer {reg.json()['token']}"}
    assert (
        client.post("/api/accounts/codex-login", json={}, headers=worker).status_code
        == 403
    )
    res = client.post("/api/accounts/codex-login", json={}, headers=ADMIN)
    assert (
        res.status_code == 409
        and res.json()["detail"]["code"] == "codex_login_unavailable"
    )
    assert os.environ.get("ARGUS_CODEX_BIN") is None


# -- catalogue and fallback ----------------------------------------------------------

HARD = RouteRequest(summary="plan the migration")
CODE = RouteRequest(summary="fix the bug", is_code=True)


def ledger(*engines: str) -> dict:
    ok = {"ok": True, "detail": "checked"}
    return {e: {"installed": ok, "validated": ok} for e in engines}


def counts(**by_provider: tuple[int, ...]) -> dict[str, AccountsSummary]:
    """provider -> (available, exhausted, disabled[, best available priority])."""
    return {
        p: AccountsSummary(
            available=v[0],
            exhausted=v[1],
            disabled=v[2],
            best_priority=(v[3] if len(v) > 3 else 0) if v[0] else None,
        )
        for p, v in by_provider.items()
    }


def route(request: RouteRequest, accounts: dict, engines=("codex", "claude")):
    cat = catalog_mod.build(ledger(*engines), None, None, accounts=accounts)
    return routing.route(request, cat), cat


def test_registry_makes_authenticated_true_and_unknown_quota_is_not_zero() -> None:
    d, cat = route(HARD, counts(openai=(1, 0, 0, 10), anthropic=(1, 0, 0, 10)))
    codex = next(e for e in cat.engines if e.id == "codex")
    assert codex.readiness.authenticated.ok is True and codex.usable
    assert (d.target, d.fallback_from, d.blocked) == ("claude", None, None)  # tie: id


def test_the_highest_priority_account_picks_the_engine_across_providers() -> None:
    d, _ = route(HARD, counts(openai=(1, 0, 0, 10), anthropic=(1, 0, 0, 100)))
    assert (d.target, d.blocked) == ("claude", None)
    d, _ = route(HARD, counts(openai=(1, 0, 0, 100), anthropic=(1, 0, 0, 10)))
    assert (d.target, d.blocked) == ("codex", None)  # no provider is favoured by name


def test_no_codex_account_routes_to_claude_and_says_why() -> None:
    d, cat = route(HARD, counts(anthropic=(1, 0, 0, 100)))
    codex = next(e for e in cat.engines if e.id == "codex")
    assert codex.readiness.authenticated.ok is False and not codex.usable
    assert d.target == "claude" and d.blocked is None
    assert d.fallback_from is None  # nothing was passed over: codex has no account


def test_exhausted_or_disabled_engine_accounts_are_passed_over() -> None:
    d, cat = route(HARD, counts(openai=(0, 2, 1), anthropic=(2, 0, 0, 100)))
    codex = next(e for e in cat.engines if e.id == "codex")
    assert codex.readiness.authenticated.ok is True  # credentials exist
    assert codex.accounts == AccountsSummary(available=0, exhausted=2, disabled=1)
    assert d.target == "claude" and "2 exhausted" in (d.fallback_from or "")
    d, _ = route(HARD, counts(openai=(1, 0, 0, 100), anthropic=(0, 2, 1)))
    assert d.target == "codex" and "2 exhausted" in (d.fallback_from or "")
    assert "fell back from claude" in d.reason


def test_code_delegates_to_the_best_executor_when_it_is_not_the_engine() -> None:
    d, _ = route(CODE, counts(openai=(1, 0, 0, 100), anthropic=(1, 0, 0, 10)))
    assert (d.target, d.blocked, d.executor) == ("codex", None, None)  # works directly
    d, _ = route(CODE, counts(openai=(1, 0, 0, 10)), engines=("codex",))
    assert (d.target, d.blocked, d.executor) == ("codex", None, None)
    plain, _ = route(HARD, counts(openai=(1, 0, 0), anthropic=(1, 0, 0)))
    assert plain.executor is None  # only code tasks delegate


def test_a_busy_top_account_is_not_routed_to_but_a_free_lower_one_is() -> None:
    busy = AccountsSummary(
        available=1, busy=1, leasable=0, best_priority=None, exhausted=0, disabled=0
    )
    free = AccountsSummary(available=1, leasable=1, best_priority=0)
    d, cat = route(HARD, {"anthropic": busy, "openai": free})
    claude = next(e for e in cat.engines if e.id == "claude")
    assert not claude.usable
    assert (d.target, d.blocked) == ("codex", None)
    d, _ = route(HARD, {"anthropic": busy, "openai": busy})
    assert d.blocked and "at capacity" in d.blocked


def test_code_without_a_claude_account_runs_on_codex() -> None:
    d, _ = route(CODE, counts(openai=(1, 0, 0, 10)))
    assert (d.target, d.blocked) == ("codex", None)


def test_code_with_no_account_at_all_is_blocked() -> None:
    d, _ = route(CODE, counts())
    assert d.blocked and d.fallback_from is None


def test_no_account_anywhere_is_blocked_not_swapped() -> None:
    d, _ = route(HARD, counts())
    assert d.blocked and d.fallback_from is None


def test_non_account_gaps_skip_the_engine_but_never_block_a_usable_one() -> None:
    # codex has the best account but was never validated: it is not usable, so the
    # usable claude runs the task; with claude unusable too the task is blocked.
    cat = catalog_mod.build(
        {
            "codex": {"installed": {"ok": True, "detail": "x"}},
            **ledger("claude"),
        },
        None,
        None,
        accounts=counts(openai=(1, 0, 0, 100), anthropic=(1, 0, 0, 10)),
    )
    d = routing.route(HARD, cat)
    assert (d.target, d.blocked, d.fallback_from) == ("claude", None, None)
    only_codex = catalog_mod.build(
        {"codex": {"installed": {"ok": True, "detail": "x"}}},
        None,
        None,
        accounts=counts(openai=(1, 0, 0, 100)),
    )
    blocked = routing.route(HARD, only_codex)
    assert blocked.blocked and blocked.fallback_from is None


def test_unknown_authentication_without_a_registry_is_not_fallback_material() -> None:
    cat = catalog_mod.build(ledger("codex", "claude"), None, None)
    codex = next(e for e in cat.engines if e.id == "codex")
    assert codex.accounts is None and codex.readiness.authenticated.ok is None
    d = routing.route(HARD, cat)
    assert d.blocked and d.fallback_from is None
    assert Fact(ok=None).ok is None


def test_catalogue_over_rest_reads_the_registry(client: TestClient) -> None:
    def codex_entry() -> dict:
        body = client.get("/api/engines", headers=ADMIN).json()
        return next(e for e in body["engines"] if e["id"] == "codex")

    assert codex_entry()["readiness"]["authenticated"]["ok"] is False
    assert codex_entry()["accounts"] == {
        "available": 0,
        "exhausted": 0,
        "disabled": 0,
        "busy": 0,
        "no_credential": 0,
        "leasable": 0,
        "best_priority": None,
    }
    res = client.post(
        "/api/accounts",
        json={"name": "c", "provider": "openai", "credential": auth_doc()},
        headers=ADMIN,
    )
    assert res.status_code == 201
    entry = codex_entry()
    assert entry["readiness"]["authenticated"]["ok"] is True
    assert entry["accounts"]["available"] == 1
    assert SECRET_REFRESH not in json.dumps(entry)
    # installed/validated are not implied by authentication
    assert entry["usable"] is False
    client.patch(
        f"/api/accounts/{res.json()['id']}", json={"enabled": False}, headers=ADMIN
    )
    assert codex_entry()["accounts"] == {
        "available": 0,
        "exhausted": 0,
        "disabled": 1,
        "busy": 0,
        "no_credential": 0,
        "leasable": 0,
        "best_priority": None,
    }


def test_engine_selection_is_recorded_for_agents_only(client: TestClient) -> None:
    body = {"engine": "codex", "provider": "openai", "account_id": "acc-1"}
    assert (
        client.post("/api/engines/selected", json=body, headers=ADMIN).status_code
        == 403
    )
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    worker = {"Authorization": f"Bearer {reg.json()['token']}"}
    assert (
        client.post("/api/engines/selected", json=body, headers=worker).status_code
        == 204
    )
    bad = {**body, "engine": "opencode"}
    assert (
        client.post("/api/engines/selected", json=bad, headers=worker).status_code
        == 422
    )
    logged = client.get(
        "/api/events", params={"kind": "engine.selected"}, headers=ADMIN
    ).text
    assert "acc-1" in logged and "openai" in logged


# -- routing follows what a lease could really take (capacity, credentials) -----------

LEDGER_OK = {"ok": True, "detail": "checked"}


def _routed(client: TestClient) -> dict:
    res = client.post("/api/engines/route", json={"summary": "plan"}, headers=ADMIN)
    assert res.status_code == 200
    return res.json()


def _summary(client: TestClient, engine: str) -> dict:
    body = client.get("/api/engines", headers=ADMIN).json()
    return next(e for e in body["engines"] if e["id"] == engine)["accounts"]


def _worker(client: TestClient) -> dict:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {reg.json()['token']}"}


def _lease(client: TestClient, worker: dict) -> dict:
    client.post(
        "/api/tasks",
        json={"project": "demo", "title": "account routing"},
        headers=ADMIN,
    )
    task = client.post("/api/tasks/claim", json={}, headers=worker).json()
    return client.post(
        "/api/accounts/lease",
        json={
            "runtime_providers": ["anthropic", "openai"],
            "task_id": task["id"],
            "attempt": task["attempt"],
        },
        headers=worker,
    ).json()


def test_a_full_top_priority_account_does_not_win_routing_over_a_free_one(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "engines-ledger.json").write_text(
        json.dumps(
            {
                "engines": {
                    e: {"installed": LEDGER_OK, "validated": LEDGER_OK}
                    for e in ("codex", "claude")
                }
            }
        )
    )
    claude = client.post(
        "/api/accounts",
        json={
            "name": "claude-top",
            "provider": "anthropic",
            "priority": 100,
            "max_leases": 1,
            "credential": {"api_key": "sk-ant-" + "api03-" + "TOPKEY" * 8},
        },
        headers=ADMIN,
    ).json()
    client.post(
        "/api/accounts",
        json={
            "name": "codex-low",
            "provider": "openai",
            "priority": 0,
            "credential": auth_doc(),
        },
        headers=ADMIN,
    )
    assert _routed(client)["target"] == "claude"  # free and highest priority
    worker = _worker(client)
    lease = _lease(client, worker)
    assert lease["name"] == "claude-top"
    # The top account is now full: lifecycle says available, capacity says busy.
    anthropic = _summary(client, "claude")
    assert (anthropic["available"], anthropic["busy"], anthropic["leasable"]) == (
        1,
        1,
        0,
    )
    assert anthropic["best_priority"] is None
    entry = next(
        e
        for e in client.get("/api/engines", headers=ADMIN).json()["engines"]
        if e["id"] == "claude"
    )
    assert entry["usable"] is False
    decision = _routed(client)
    assert (decision["target"], decision["blocked"]) == ("codex", None)
    # What a lease really does agrees with the route.
    other = _worker(client)
    assert _lease(client, other)["name"] == "codex-low"
    # Releasing the slot makes the top account leasable and routable again.
    client.post("/api/accounts/release", headers=worker)
    assert _summary(client, "claude")["leasable"] == 1
    assert _routed(client)["target"] == "claude"
    assert claude["id"]


def test_a_missing_secret_is_not_leasable_and_routing_never_writes_or_leaks(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "engines-ledger.json").write_text(
        json.dumps(
            {
                "engines": {
                    e: {"installed": LEDGER_OK, "validated": LEDGER_OK}
                    for e in ("codex", "claude")
                }
            }
        )
    )
    key = "sk-ant-" + "api03-" + "GONEKEY" * 8
    created = client.post(
        "/api/accounts",
        json={
            "name": "claude-top",
            "provider": "anthropic",
            "priority": 100,
            "credential": {"api_key": key},
        },
        headers=ADMIN,
    ).json()
    client.post(
        "/api/accounts",
        json={"name": "codex-low", "provider": "openai", "credential": auth_doc()},
        headers=ADMIN,
    )
    secret = created["secret_ref"].removeprefix("sops:")
    store = app.state.secrets
    del store._data[secret]  # the secret vanishes behind Argus's back
    before = client.get(f"/api/accounts/{created['id']}", headers=ADMIN).json()
    assert _routed(client)["target"] == "codex"
    summary = _summary(client, "claude")
    assert (summary["available"], summary["no_credential"], summary["leasable"]) == (
        1,
        1,
        0,
    )
    # Read-only: the account row is untouched by the catalogue and the route...
    after = client.get(f"/api/accounts/{created['id']}", headers=ADMIN).json()
    assert after["state"] == before["state"] == "available"
    assert after["updated_at"] == before["updated_at"]
    # ...and nothing secret appears in what they return.
    leaked = client.get("/api/engines", headers=ADMIN).text + json.dumps(
        _routed(client)
    )
    assert key not in leaked and SECRET_REFRESH not in leaked
    # A lease agrees: it skips the account (and only then disables it).
    worker = _worker(client)
    assert _lease(client, worker)["name"] == "codex-low"
