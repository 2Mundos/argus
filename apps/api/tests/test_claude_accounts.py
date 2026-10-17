"""Claude accounts: credential validation over REST, renewal, and the dedicated
login driven against a fake `claude setup-token` on a pseudo-terminal (claude
accounts spec). No real client, no network, no host credentials."""

import asyncio
import contextlib
import json
import os
import signal
import stat
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from argus_api.accounts import login_base, service
from argus_api.accounts.claude_login import (
    ClaudeLoginManager,
    LoginState,
    LoginUnavailableError,
    url_allowed,
)
from argus_api.config import Settings
from argus_api.db import Database
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from fastapi.testclient import TestClient

ADMIN_TOKEN = "t" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

# Built at runtime so no literal looks like a real credential to a scanner.
TOKEN = "sk-ant-" + "oat01-" + "FAKE" * 15 + "-SECRET"
API_KEY = "sk-ant-" + "api03-" + "OLDKEY" * 8
CODE = "AbC123-xyz_9.~#st%2Bate"
OLD = {"api_key": API_KEY}

FAKE = """\
#!{python}
import json, os, subprocess, sys, time
mode = {mode!r}
CODE_OK = {code!r}
report = {report!r}
with open(report, "w") as f:
    json.dump({{"env": dict(os.environ), "cwd": os.getcwd(), "isatty": sys.stdin.isatty()}}, f)
url = "https://evil.example/oauth/authorize?x=1" if mode == "badurl" else (
    "https://claude.ai/oauth/authorize?code=true&client_id=c&state=ST"
)
if mode == "nourl":
    sys.stdout.write("hello\\n"); sys.stdout.flush(); time.sleep(60)
if mode in ("tree", "stubborn"):
    code = "import signal,time;" + (
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);" if mode == "stubborn" else ""
    ) + "time.sleep(30)"
    child = subprocess.Popen([sys.executable, "-c", code])
    open({pids!r}, "w").write(str(child.pid))
sys.stdout.write("\\x1b[2J\\x1b[1mOpen this URL to sign in:\\x1b[0m\\n")
sys.stdout.write("\\x1b]8;;" + url + "\\x07" + url + "\\x1b]8;;\\x07\\n")
sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
if mode in ("hang", "tree", "stubborn", "badurl"):
    time.sleep(60)
if mode == "immediate":
    sys.stdout.write("Your OAuth token:\\n\\x1b[32m" + {token!r} + "\\x1b[0m\\nStore it safely\\n")
    sys.exit(0)
if mode == "raw":
    import tty
    tty.setraw(0)
    # Say when the raw read is armed: in canonical mode a CR ends the line and the
    # read would return the code without it, which says nothing about the manager.
    open({ready!r}, "w").write("1")
    open({received!r}, "w").write(repr(os.read(0, 4096)))
    sys.stdout.write("OAuth error: Request failed with status code 400\\n"); sys.stdout.flush()
    time.sleep(60)
if mode in ("retry", "stubborn_oauth", "silent", "no_token"):
    tries = []
    while True:
        line = sys.stdin.readline().rstrip("\\r\\n")
        tries.append(line)
        open({received!r}, "w").write("\\n".join(tries))
        if mode == "silent":
            sys.exit(1)
        if mode == "no_token":
            sys.stdout.write("\\x1b[32m✓ Long-lived authentication token created successfully!\\n")
            sys.stdout.write("Your OAuth token (valid for 1 year):\\nnot-a-token\\n"); sys.stdout.flush()
            sys.exit(0)
        if mode == "retry" and len(tries) == 2:
            break
        sys.stdout.write("OAuth error: Request failed with status code 400 Press Enter to retry.\\n")
        sys.stdout.flush()
    line = CODE_OK
else:
    line = sys.stdin.readline().rstrip("\\r\\n")
open({received!r}, "w").write(line)
if mode == "reject" or line != {code!r}:
    sys.stdout.write("Invalid code. Authentication failed: " + line + "\\n"); sys.stdout.flush()
    sys.exit(1)
if mode == "real":
    # Exactly what the shipped 2.1.296 client prints, with the token last.
    sys.stdout.write("Your OAuth token (valid for 31536000):\\n\\x1b[33m" + {token!r} + "\\x1b[0m")
    sys.stdout.flush()
    sys.exit(0)
while not os.path.exists({go!r}):
    time.sleep(0.02)
sys.stdout.write("\\nLong-lived token (valid for 1 year):\\n\\x1b[32m" + {token!r} + "\\x1b[0m\\n\\nStore it safely\\n")
sys.stdout.flush()
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        app.state.secrets = MemorySecretStore()
        yield c


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


def fake_claude(
    tmp_path: Path, mode: str = "ok", token: str = TOKEN
) -> tuple[str, Path]:
    go = tmp_path / "go"
    go.write_text("")  # tests that need a pause remove it
    script = tmp_path / f"claude-{mode}"
    script.write_text(
        FAKE.format(
            python=sys.executable,
            mode=mode,
            report=str(tmp_path / "report.json"),
            received=str(tmp_path / "received"),
            ready=str(tmp_path / "ready"),
            pids=str(tmp_path / "child.pid"),
            go=str(go),
            token=token,
            code=CODE,
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), go


def manager(
    db: Database, secrets, tmp_path: Path, mode: str = "ok", token: str = TOKEN, **kw
):
    binary, go = fake_claude(tmp_path, mode, token)
    settings = Settings(data_dir=tmp_path, claude_bin=binary)
    return ClaudeLoginManager(db, secrets, settings, **kw), go


def homes(tmp_path: Path) -> set[Path]:
    return set((tmp_path / "tmp").glob("argus-claude-login-*"))


async def settle(mgr: ClaudeLoginManager, login_id: str, owner: str = "tok-1") -> dict:
    for _ in range(300):
        view = mgr.get(login_id, owner)
        if view["state"] != "pending":
            return view
        await asyncio.sleep(0.02)
    return view


async def seed(db: Database, secrets, name="main", credential=OLD, **fields) -> str:
    """An existing anthropic account with settings worth preserving."""
    from argus_core.accounts import AccountCreate, AccountUpdate

    await secrets.put("seed-" + name, credential)
    body = AccountCreate(
        name=name,
        provider="anthropic",
        kind="api_key",
        priority=7,
        max_leases=3,
        labels={"team": "a"},
        secret_ref="sops:seed-" + name,
    )
    async with db.write() as tx:
        account = await service.create_account(
            tx, "acc-" + name, body, "sops:seed-" + name, "admin:one", secrets
        )
        if fields.get("disabled"):
            await service.update_account(
                tx, account.id, AccountUpdate(enabled=False), "admin:one"
            )
    return "acc-" + name


async def account_row(db: Database, account_id: str):
    async with (
        db.read() as conn,
        conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)) as cur,
    ):
        return await cur.fetchone()


async def account_count(db: Database) -> int:
    async with (
        db.read() as conn,
        conn.execute("SELECT count(*) AS n FROM accounts") as cur,
    ):
        return (await cur.fetchone())["n"]


async def all_events(db: Database) -> str:
    async with db.read() as conn, conn.execute("SELECT kind, data FROM events") as cur:
        return " ".join(f"{r['kind']} {r['data']}" for r in await cur.fetchall())


# -- import and renewal over REST ----------------------------------------------------


def test_import_derives_kind_and_never_echoes_the_credential(
    client: TestClient,
) -> None:
    for credential, kind in [
        ({"api_key": API_KEY}, "api_key"),
        ({"oauth_token": TOKEN}, "subscription"),
    ]:
        res = client.post(
            "/api/accounts",
            json={
                "name": f"c-{kind}",
                "provider": "anthropic",
                "kind": "api_key",
                "credential": credential,
            },
            headers=ADMIN,
        )
        assert res.status_code == 201, res.text
        assert res.json()["kind"] == kind  # derived, whatever the form sent
        assert TOKEN not in res.text and API_KEY not in res.text
        assert TOKEN not in client.get("/api/accounts", headers=ADMIN).text


@pytest.mark.parametrize(
    ("credential", "needle"),
    [
        ({"token": TOKEN}, "needs one of"),
        ({"api_key": TOKEN}, "use oauth_token"),
        ({"claudeAiOauth": {"accessToken": TOKEN}}, "missing: refreshToken"),
        (
            {
                "claudeAiOauth": {
                    "accessToken": TOKEN,
                    "refreshToken": "r" * 20,
                    "expiresAt": 1000,
                }
            },
            "in the past",
        ),
    ],
)
def test_import_rejects_with_field_names_only(
    client: TestClient, credential, needle
) -> None:
    res = client.post(
        "/api/accounts",
        json={"name": "bad", "provider": "anthropic", "credential": credential},
        headers=ADMIN,
    )
    assert res.status_code == 422
    assert needle in res.text and TOKEN not in res.text


def test_renewing_over_rest_keeps_settings_and_syncs_the_kind(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/accounts",
        json={
            "name": "main",
            "provider": "anthropic",
            "priority": 7,
            "max_leases": 3,
            "labels": {"team": "a"},
            "credential": {"api_key": API_KEY},
        },
        headers=ADMIN,
    ).json()
    client.patch(
        f"/api/accounts/{created['id']}", json={"enabled": False}, headers=ADMIN
    )
    res = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={"credential": {"oauth_token": TOKEN}},
        headers=ADMIN,
    )
    assert res.status_code == 204
    after = client.get(f"/api/accounts/{created['id']}", headers=ADMIN).json()
    assert (after["name"], after["priority"], after["max_leases"], after["labels"]) == (
        "main",
        7,
        3,
        {"team": "a"},
    )
    assert after["kind"] == "subscription"
    assert after["state"] == "disabled"  # an intentional opt-out stays
    assert app.state.secrets.get(created["secret_ref"].removeprefix("sops:")) == {
        "oauth_token": TOKEN
    }
    assert TOKEN not in json.dumps(after)
    bad = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={"credential": {"token": TOKEN}},
        headers=ADMIN,
    )
    assert bad.status_code == 422 and TOKEN not in bad.text


@pytest.mark.parametrize(
    ("credential", "needle"),
    [
        ({"oauth_token": "A" * 48 + "#" + "B" * 43}, "sign-in page shows"),
        ({"oauth_token": "z" * 92}, "oauth_token is not a token"),
    ],
)
def test_a_value_that_is_not_a_token_is_refused_and_stores_nothing(
    client: TestClient, credential, needle
) -> None:
    created = client.post(
        "/api/accounts",
        json={
            "name": "main",
            "provider": "anthropic",
            "credential": {"api_key": API_KEY},
        },
        headers=ADMIN,
    ).json()
    ref = created["secret_ref"].removeprefix("sops:")
    before = app.state.secrets.get(ref)

    res = client.put(
        f"/api/accounts/{created['id']}/credential",
        json={"credential": credential},
        headers=ADMIN,
    )
    assert res.status_code == 422 and needle in res.text
    assert credential["oauth_token"] not in res.text
    assert app.state.secrets.get(ref) == before == {"api_key": API_KEY}
    after = client.get(f"/api/accounts/{created['id']}", headers=ADMIN).json()
    assert after["secret_ref"] == created["secret_ref"]

    count = len(client.get("/api/accounts", headers=ADMIN).json())
    res = client.post(
        "/api/accounts",
        json={"name": "new", "provider": "anthropic", "credential": credential},
        headers=ADMIN,
    )
    assert res.status_code == 422 and needle in res.text
    assert len(client.get("/api/accounts", headers=ADMIN).json()) == count


# -- the dedicated login -------------------------------------------------------------


async def test_login_completes_into_an_account(
    db: Database, secrets, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "HERMES_HOME",
    ):
        monkeypatch.setenv(var, "host-secret")
    mgr, _ = manager(db, secrets, tmp_path)
    started = await mgr.start("tok-1", "admin:one", "Claude main")
    assert started["state"] == "pending" and started["phase"] == "waiting_for_code"
    assert started["url"].startswith("https://claude.ai/oauth/authorize")
    (home,) = homes(tmp_path)
    assert oct(home.stat().st_mode & 0o777) == "0o700"

    report = json.loads((tmp_path / "report.json").read_text())
    env = report["env"]
    assert report["isatty"] is True
    assert report["cwd"] == str(home.resolve())
    for var in (
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        assert env[var].startswith(str(home)), var
        assert oct(Path(env[var]).stat().st_mode & 0o777) == "0o700"
    assert not {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "HERMES_HOME",
    } & set(env)
    assert env["BROWSER"].startswith(str(home))

    view = await mgr.submit_code(started["login_id"], "tok-1", CODE)
    assert view["phase"] == "finishing"
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "completed", view
    assert (tmp_path / "received").read_text() == CODE  # forwarded byte for byte
    assert view["url"] is None and view["phase"] is None
    row = await account_row(db, view["account_id"])
    assert (row["provider"], row["kind"]) == ("anthropic", "subscription")
    assert row["name"].startswith("Claude main ")
    assert secrets.get(row["secret_ref"].removeprefix("sops:")) == {
        "oauth_token": TOKEN
    }
    shown = json.dumps(view) + await all_events(db)
    assert TOKEN not in shown and CODE not in shown and "oauth/authorize" not in shown
    assert homes(tmp_path) == set()


@pytest.mark.parametrize(
    "token",
    [
        TOKEN,
        "sk-ant-" + "oat-" + "NOVER" * 12,  # the client's own prefix test, no version
    ],
)
async def test_the_real_client_output_is_parsed(
    db: Database, secrets, tmp_path: Path, token: str
) -> None:
    """The token the shipped client prints last, standalone: the parser keys on the
    `sk-ant-oat` prefix the client itself checks, never on an assumed version."""
    mgr, _ = manager(db, secrets, tmp_path, mode="real", token=token)
    started = await mgr.start("tok-1", "admin:one", None)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "completed", view
    row = await account_row(db, view["account_id"])
    assert secrets.get(row["secret_ref"].removeprefix("sops:")) == {
        "oauth_token": token
    }


async def test_renewal_keeps_the_account_and_replaces_only_the_credential(
    db: Database, secrets, tmp_path: Path
) -> None:
    account_id = await seed(db, secrets, disabled=True)
    before = await account_row(db, account_id)
    mgr, _ = manager(db, secrets, tmp_path)
    started = await mgr.start("tok-1", "admin:one", "ignored", renew_id=account_id)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "completed" and view["account_id"] == account_id
    after = await account_row(db, account_id)
    for column in (
        "name",
        "priority",
        "max_leases",
        "labels",
        "state",
        "secret_ref",
        "disabled_reason",
    ):
        assert after[column] == before[column], column
    assert after["state"] == "disabled" and after["kind"] == "subscription"
    assert secrets.get("seed-main") == {"oauth_token": TOKEN}
    assert await account_count(db) == 1
    events = await all_events(db)
    assert (
        "credential_renewed" in events and TOKEN not in events and API_KEY not in events
    )


async def test_renewal_targets_must_be_anthropic_accounts(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path)
    with pytest.raises(NotFoundError):
        await mgr.start("tok-1", "admin:one", None, renew_id="missing")
    assert homes(tmp_path) == set()


async def test_renewal_that_cannot_be_written_restores_the_old_credential(
    db: Database, secrets, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = await seed(db, secrets)

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "credential_renewed", boom)
    mgr, _ = manager(db, secrets, tmp_path)
    started = await mgr.start("tok-1", "admin:one", None, renew_id=account_id)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "failed" and view["error"] == "RuntimeError"
    assert secrets.get("seed-main") == OLD
    assert (await account_row(db, account_id))["kind"] == "api_key"


async def test_login_is_isolated_by_owner_and_limited(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="hang")
    first = await mgr.start("tok-1", "admin:one", None)
    with pytest.raises(NotFoundError):
        mgr.get(first["login_id"], "tok-2")
    with pytest.raises(NotFoundError):
        await mgr.submit_code(first["login_id"], "tok-2", CODE)
    with pytest.raises(NotFoundError):
        await mgr.cancel(first["login_id"], "tok-2")
    with pytest.raises(ConflictError) as err:
        await mgr.start("tok-1", "admin:one", None)
    assert err.value.code == "login_in_progress"
    await mgr.start("tok-2", "admin:two", None)
    with pytest.raises(ConflictError) as err:
        await mgr.start("tok-3", "admin:three", None)
    assert err.value.code == "login_limit"
    await mgr.shutdown()


@pytest.mark.parametrize(
    "code",
    ["", " " + CODE, CODE + "\n", "a b", "é", "x" * 2049, "$(id)", "a;b", "a\x03"],
)
async def test_malformed_codes_are_refused_without_echo(
    db: Database, secrets, tmp_path: Path, code: str
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="hang")
    started = await mgr.start("tok-1", "admin:one", None)
    with pytest.raises(ValidationFailedError) as err:
        await mgr.submit_code(started["login_id"], "tok-1", code)
    assert code.strip() not in err.value.message or not code.strip()
    # Refusing does not use up the one code.
    assert mgr.get(started["login_id"], "tok-1")["phase"] == "waiting_for_code"
    await mgr.shutdown()


async def test_no_code_is_taken_once_the_login_is_over(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, go = manager(db, secrets, tmp_path)
    go.unlink()
    started = await mgr.start("tok-1", "admin:one", None)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    go.write_text("")
    assert (await settle(mgr, started["login_id"]))["state"] == "completed"
    with pytest.raises(ConflictError) as err:
        await mgr.submit_code(started["login_id"], "tok-1", CODE)
    assert err.value.code == "login_closed"


async def test_a_refused_code_fails_the_login_without_leaking_output(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="reject")
    started = await mgr.start("tok-1", "admin:one", None)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "failed" and view["error"] == "code_invalid"
    assert CODE not in json.dumps(view)  # the client echoed it; we do not
    assert secrets.names() == set() and await account_count(db) == 0
    assert homes(tmp_path) == set()


async def test_a_client_that_leaves_silently_after_a_code_is_login_rejected(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="silent")
    started = await mgr.start("tok-1", "admin:one", None)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "failed" and view["error"] == "login_rejected"
    assert secrets.names() == set() and await account_count(db) == 0


async def test_a_refused_code_can_be_followed_by_another(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="retry")
    started = await mgr.start("tok-1", "admin:one", None)
    login_id = started["login_id"]
    assert started["attempts"] == 0 and started["max_attempts"] == 3
    await mgr.submit_code(login_id, "tok-1", "wrong-code")
    for _ in range(100):  # the client refuses but stays alive
        view = mgr.get(login_id, "tok-1")
        if view["refusal"]:
            break
        await asyncio.sleep(0.02)
    assert view["state"] == "pending" and view["phase"] == "waiting_for_code"
    assert view["refusal"] == "oauth_400" and view["attempts"] == 1
    assert "wrong-code" not in json.dumps(view)
    view = await mgr.submit_code(login_id, "tok-1", CODE)
    assert view["attempts"] == 2 and view["refusal"] is None
    view = await settle(mgr, login_id)
    assert view["state"] == "completed", view
    assert view["attempts"] == 2
    assert (tmp_path / "received").read_text() == CODE
    row = await account_row(db, view["account_id"])
    assert secrets.get(row["secret_ref"].removeprefix("sops:")) == {
        "oauth_token": TOKEN
    }


async def test_success_without_a_recognisable_token_is_token_not_found(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="no_token")
    started = await mgr.start("tok-1", "admin:one", None)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "failed" and view["error"] == "token_not_found"
    assert secrets.names() == set() and await account_count(db) == 0


async def test_the_number_of_codes_is_limited(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="stubborn_oauth")
    started = await mgr.start("tok-1", "admin:one", None)
    login_id = started["login_id"]
    for n in range(1, 4):
        view = await mgr.submit_code(login_id, "tok-1", f"code{n}")
        assert view["attempts"] == n
        for _ in range(100):
            if mgr.get(login_id, "tok-1")["refusal"]:
                break
            await asyncio.sleep(0.02)
    with pytest.raises(ConflictError) as err:
        await mgr.submit_code(login_id, "tok-1", "code4")
    assert err.value.code == "login_attempts_exhausted"
    assert mgr.get(login_id, "tok-1")["attempts"] == 3
    await mgr.shutdown()


async def test_the_code_and_enter_arrive_in_one_write(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="raw")
    started = await mgr.start("tok-1", "admin:one", None)
    for _ in range(100):  # the client must be reading raw before the code is sent
        if (tmp_path / "ready").exists():
            break
        await asyncio.sleep(0.02)
    assert (tmp_path / "ready").exists()
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    for _ in range(100):
        if (tmp_path / "received").exists() and (tmp_path / "received").read_text():
            break
        await asyncio.sleep(0.02)
    assert (tmp_path / "received").read_text() == repr(CODE.encode() + b"\r")
    await mgr.shutdown()


async def test_a_token_printed_without_a_code_is_still_taken(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="immediate")
    started = await mgr.start("tok-1", "admin:one", None)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "completed"


async def test_cancel_kills_the_client_and_removes_its_home(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="hang")
    started = await mgr.start("tok-1", "admin:one", None)
    session = mgr._sessions[started["login_id"]]
    home, proc = session.home, session.proc
    assert home is not None and home.exists() and proc is not None
    view = await mgr.cancel(started["login_id"], "tok-1")
    assert view["state"] == LoginState.CANCELLED and view["url"] is None
    assert not home.exists() and proc.returncode is not None and session.master is None
    assert mgr.get(started["login_id"], "tok-1")["state"] == "cancelled"
    with pytest.raises(ConflictError):
        await mgr.submit_code(started["login_id"], "tok-1", CODE)
    await mgr.start("tok-1", "admin:one", None)  # the owner may start another
    await mgr.shutdown()


async def test_login_expires_and_a_late_token_creates_nothing(
    db: Database, secrets, tmp_path: Path
) -> None:
    now = [0.0]
    mgr, go = manager(db, secrets, tmp_path, clock=lambda: now[0])
    go.unlink()
    started = await mgr.start("tok-1", "admin:one", None)
    assert started["expires_in_s"] == 900
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    now[0] = 901
    assert mgr.get(started["login_id"], "tok-1")["state"] == "expired"
    go.write_text("")  # the client would now print its token
    await asyncio.sleep(0.4)
    view = mgr.get(started["login_id"], "tok-1")
    assert view["state"] == "expired" and view["account_id"] is None
    assert secrets.names() == set() and await account_count(db) == 0
    await mgr.shutdown()
    with pytest.raises(ConflictError):
        await mgr.submit_code(started["login_id"], "tok-1", CODE)


@pytest.mark.parametrize("mode", ["badurl", "nourl"])
async def test_a_foreign_or_missing_url_aborts_the_login(
    db: Database, secrets, tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode=mode)
    if mode == "nourl":
        monkeypatch.setattr("argus_api.accounts.claude_login.START_TIMEOUT_S", 0.5)
    with pytest.raises(LoginUnavailableError) as err:
        await mgr.start("tok-1", "admin:one", None)
    assert ("url_not_allowed" if mode == "badurl" else "start_timeout") in str(
        err.value
    )
    assert "evil.example" not in str(err.value)
    assert homes(tmp_path) == set()


async def test_unavailable_without_a_client(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr = ClaudeLoginManager(db, secrets, Settings(data_dir=tmp_path))
    with pytest.raises(LoginUnavailableError):
        await mgr.start("tok-1", "admin:one", None)


class GatedStore(MemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def put(self, name: str, value: dict) -> None:
        self.entered.set()
        await self.release.wait()
        await super().put(name, value)


async def test_cancel_and_expiry_during_persistence_report_the_account(
    db: Database, tmp_path: Path
) -> None:
    now = [0.0]
    store = GatedStore()
    mgr, _ = manager(db, store, tmp_path, clock=lambda: now[0])
    started = await mgr.start("tok-1", "admin:one", None)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    await asyncio.wait_for(store.entered.wait(), 5)
    now[0] = 901  # expiry and a cancel both land while the account is written
    assert mgr.get(started["login_id"], "tok-1")["state"] == "pending"
    cancelling = asyncio.create_task(mgr.cancel(started["login_id"], "tok-1"))
    await asyncio.sleep(0.1)
    assert not cancelling.done()  # it waits for the outcome
    store.release.set()
    view = await cancelling
    assert view["state"] == "completed" and view["account_id"]
    assert await account_count(db) == 1


async def test_a_failed_account_write_leaves_no_secret(
    db: Database, secrets, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "create_account", boom)
    mgr, _ = manager(db, secrets, tmp_path)
    started = await mgr.start("tok-1", "admin:one", None)
    await mgr.submit_code(started["login_id"], "tok-1", CODE)
    view = await settle(mgr, started["login_id"])
    assert view["state"] == "failed"
    assert secrets.names() == set() and await account_count(db) == 0


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


async def _gone(pid: int, seconds: float = 8) -> bool:
    for _ in range(int(seconds / 0.05)):
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


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
    monkeypatch.setattr(login_base, "_TERM_GRACE_S", 0.3)
    now = [0.0]
    mgr, _ = manager(db, secrets, tmp_path, mode=mode, clock=lambda: now[0])
    started = await mgr.start("tok-1", "admin:one", None)
    child = int((tmp_path / "child.pid").read_text())
    try:
        assert _alive(child)
        if how == "cancel":
            await mgr.cancel(started["login_id"], "tok-1")
        elif how == "shutdown":
            await mgr.shutdown()
        else:
            now[0] = 901
            assert mgr.get(started["login_id"], "tok-1")["state"] == "expired"
            await mgr.shutdown()
        assert await _gone(child)
        assert homes(tmp_path) == set()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
        await mgr.shutdown()


async def test_concurrent_cleanup_callers_all_wait_for_the_one_teardown(
    db: Database, secrets, tmp_path: Path
) -> None:
    mgr, _ = manager(db, secrets, tmp_path, mode="hang")
    started = await mgr.start("tok-1", "admin:one", None)
    session = mgr._sessions[started["login_id"]]
    home = session.home
    calls = [asyncio.create_task(mgr._kill(session)) for _ in range(3)]
    calls.append(asyncio.create_task(mgr.cancel(started["login_id"], "tok-1")))
    first = calls[0]
    first.cancel()  # a cancelled caller must not abandon the teardown
    await asyncio.gather(*calls, return_exceptions=True)
    assert home is not None and not home.exists()
    assert session.proc is None and session.master is None


def test_docker_mode_is_locked_down(db: Database, secrets, tmp_path: Path) -> None:
    from argus_api.accounts.claude_login import _Session

    settings = Settings(data_dir=tmp_path, claude_login_image="agent:1")
    mgr = ClaudeLoginManager(db, secrets, settings)
    s = _Session(id="a" * 32, owner="o", name="n", expires_at=1.0)
    s.home = tmp_path / "h"
    argv, env = mgr._command(s)
    joined = " ".join(argv)
    assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv
    assert f"{os.getuid()}:{os.getgid()}" in argv
    mounts = [argv[k + 1] for k, a in enumerate(argv) if a == "-v"]
    assert mounts == [f"{s.home}:/claude-home"]  # the private home, nothing else
    assert "docker.sock" not in joined and "--privileged" not in joined
    assert argv[-4:] == ["--entrypoint", "claude", "agent:1", "setup-token"]
    assert "ANTHROPIC" not in joined and set(env) == {"PATH"}
    assert s.container == "argus-claude-login-" + "a" * 12


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://claude.ai/oauth/authorize?x=1", True),
        ("https://platform.claude.com/oauth/authorize", True),
        ("https://console.anthropic.com/x", True),
        ("http://claude.ai/x", False),
        ("https://claude.ai.evil.example/x", False),
        ("https://evilclaude.ai/x", False),
        ("https://user:pw@claude.ai/x", False),
        ("https://claude.ai@evil.example/x", False),
        ("javascript:alert(1)", False),
        ("https://localhost/x", False),
    ],
)
def test_url_allowlist(url: str, ok: bool) -> None:
    assert url_allowed(url) is ok


def test_login_routes_need_admin_and_never_echo_the_code(
    client: TestClient, tmp_path: Path
) -> None:
    boot = client.post("/api/agents/bootstrap-tokens", json={}, headers=ADMIN).json()
    reg = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    worker = {"Authorization": f"Bearer {reg.json()['token']}"}
    for method, path in [
        ("post", "/api/accounts/claude-login"),
        ("get", "/api/accounts/claude-login/x"),
        ("post", "/api/accounts/claude-login/x/code"),
        ("delete", "/api/accounts/claude-login/x"),
    ]:
        kwargs = {"json": {"code": "x"}} if method == "post" else {}
        assert (
            getattr(client, method)(path, headers=worker, **kwargs).status_code == 403
        ), path
    res = client.post("/api/accounts/claude-login", json={}, headers=ADMIN)
    assert (
        res.status_code == 409
        and res.json()["detail"]["code"] == "claude_login_unavailable"
    )
    # A body of the wrong shape is a 422 that does not repeat what was sent.
    secret = "sk-ant-" + "oat01-" + "LEAK" * 12
    for body in ({"code": {"nested": secret}}, {"code": [secret]}, {"other": secret}):
        bad = client.post("/api/accounts/claude-login/x/code", json=body, headers=ADMIN)
        assert (
            bad.status_code == 422 and secret not in bad.text and "LEAK" not in bad.text
        )
    missing = client.post(
        "/api/accounts/claude-login/nope/code", json={"code": "abc"}, headers=ADMIN
    )
    assert missing.status_code == 404
