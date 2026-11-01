import hashlib
import sqlite3
from pathlib import Path

import dev
import pytest
from argus_api.db.migrate import load_migrations

T0 = "2026-10-01T00:00:00+00:00"


def tid(n: int) -> str:
    return f"t{n:03d}"


def build_source(path: Path) -> None:
    """A preview-like database at the current schema, with rows to keep and drop."""
    conn = sqlite3.connect(path)
    for m in load_migrations():
        conn.executescript(m.sql)
    conn.execute(f"PRAGMA user_version = {len(load_migrations())}")
    conn.execute("INSERT INTO projects VALUES ('p', 'P', 'd', ?, ?)", (T0, T0))
    states = ["succeeded", "failed", "succeeded", "ready", "pending", "in_review"]
    for n, state in enumerate(states):  # created_at ascends with n
        conn.execute(
            "INSERT INTO tasks (id, kind, title, spec, acceptance, input,"
            " required_capabilities, priority, state, requires_approval, review,"
            " lease_ttl_s, max_attempts, created_by, created_at, updated_at, project)"
            " VALUES (?, 'k', 't', 's', '[]', '{}', '[]', 0, ?, 0, 'none', 60, 3,"
            " 'me', ?, ?, 'p')",
            (tid(n), state, f"2026-10-01T00:00:0{n}+00:00", T0),
        )
        conn.execute(
            "INSERT INTO task_attempts (task_id, attempt, agent_id, claimed_at,"
            " definition) VALUES (?, 1, 'a1', ?, '{}')",
            (tid(n), T0),
        )
        conn.execute(
            "INSERT INTO task_log VALUES (?, 1, 1, 'worker', ?, 'line')", (tid(n), T0)
        )
        conn.execute(
            "INSERT INTO events (at, actor, subject_type, subject_id, kind)"
            " VALUES (?, 'me', 'task', ?, 'task.created')",
            (T0, tid(n)),
        )
        conn.execute(
            "INSERT INTO artifacts (id, task_id, attempt, name, media_type, size,"
            " sha256, created_by, created_at) VALUES (?, ?, 1, 'r', 'text/plain', 1,"
            " 'x', 'a1', ?)",
            (f"art{n}", tid(n), T0),
        )
    conn.execute("UPDATE tasks SET parent_id = 't000' WHERE id = 't001'")
    conn.execute("UPDATE tasks SET parent_id = 't001' WHERE id = 't002'")
    conn.execute("INSERT INTO task_deps VALUES ('t002', 't000')")
    conn.execute("INSERT INTO task_deps VALUES ('t002', 't003')")
    conn.execute(
        "INSERT INTO routines (id, project, slug, title, schedule, template, findings,"
        " created_by, created_at, updated_at) VALUES ('r', 'p', 'r', 'r', '* * * * *',"
        " '{}', 'off', 'me', ?, ?)",
        (T0, T0),
    )
    conn.execute(
        "INSERT INTO tokens (id, token_hash, kind, role, created_by, created_at)"
        " VALUES ('tok', 'h', 'admin', 'admin', 'me', ?)",
        (T0,),
    )
    conn.execute(
        "INSERT INTO accounts (id, name, provider, kind, secret_ref, state,"
        " created_at, updated_at) VALUES ('acc', 'acc', 'claude', 'api_key', 'ref',"
        " 'available', ?, ?)",
        (T0, T0),
    )
    conn.execute(
        "INSERT INTO agents (id, name, role, capabilities, state, last_heartbeat_at,"
        " created_at) VALUES ('a1', 'w', 'worker', '[]', 'active', ?, ?)",
        (T0, T0),
    )
    conn.execute(
        "INSERT INTO account_leases (id, account_id, agent_id, providers, leased_at)"
        " VALUES ('l', 'acc', 'a1', '[]', ?)",
        (T0,),
    )
    conn.execute(
        "INSERT INTO pools (name, target, runtime, image, capabilities,"
        " desired_replicas, max_replicas, config, created_at, updated_at)"
        " VALUES ('pool', 'docker', 'plain', 'img', '[]', 2, 3, '{}', ?, ?)",
        (T0, T0),
    )
    conn.execute(
        "INSERT INTO instances (id, pool, provider_ref, state, created_at)"
        " VALUES ('i', 'pool', 'c', 'running', ?)",
        (T0,),
    )
    conn.commit()
    conn.close()


def ids(db: Path, table: str, col: str = "id") -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute(f"SELECT {col} FROM {table}")}
    finally:
        conn.close()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "preview" / "argus.db"
    path.parent.mkdir()
    build_source(path)
    return path


def test_seed_keeps_history_and_drops_live_state(source: Path, tmp_path: Path) -> None:
    out = tmp_path / ".argus"
    assert dev.seed(source, out, keep_tasks=3)
    db = out / "argus.db"
    # The 3 newest non-queued tasks: t005 in_review, t002, t001. Queued ones go.
    assert ids(db, "tasks") == {"t005", "t002", "t001"}
    assert ids(db, "task_attempts", "task_id") == {"t005", "t002", "t001"}
    assert ids(db, "task_log", "task_id") == {"t005", "t002", "t001"}
    assert ids(db, "events", "subject_id") == {"t005", "t002", "t001"}
    assert ids(db, "task_deps", "depends_on_id") == set()  # t000 and t003 are gone
    assert ids(db, "projects", "slug") == {"global", "p"}  # all projects stay
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT parent_id FROM tasks WHERE id = 't001'").fetchone() == (
        None,
    )
    assert conn.execute("SELECT parent_id FROM tasks WHERE id = 't002'").fetchone() == (
        "t001",
    )
    for table in ("routines", "tokens", "account_leases", "instances", "artifacts"):
        assert ids(db, table) == set(), table
    for table in ("accounts", "account_usage", "account_quota"):
        assert ids(db, table, "rowid") == set(), table
    pool = conn.execute("SELECT desired_replicas, max_replicas FROM pools")
    assert pool.fetchone() == (0, 0)
    assert conn.execute("SELECT state FROM agents").fetchone() == ("gone",)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
    assert sorted(p.name for p in out.iterdir()) == ["argus.db"]


def test_seed_copies_and_never_touches_the_source(source: Path, tmp_path: Path) -> None:
    before = sha(source)
    out = tmp_path / ".argus"
    dev.seed(source, out)
    assert sha(source) == before
    assert (out / "argus.db").stat().st_ino != source.stat().st_ino
    assert not (out / "argus.db").is_symlink()
    assert ids(source, "routines") == {"r"}
    assert ids(source, "tokens") == {"tok"}


def test_accounts_are_copied_only_on_request_and_only_into_data_dir(
    source: Path, tmp_path: Path
) -> None:
    (source.parent / "secrets.sops.yaml").write_text("sops")
    plain = tmp_path / "plain"
    dev.seed(source, plain)
    assert ids(plain / "argus.db", "accounts") == set()
    assert not (plain / "secrets.sops.yaml").exists()

    withacc = tmp_path / "withacc"
    dev.seed(source, withacc, accounts=True)
    assert ids(withacc / "argus.db", "accounts") == {"acc"}
    assert ids(withacc / "argus.db", "account_leases") == set()  # leases never
    assert (withacc / "secrets.sops.yaml").read_text() == "sops"
    assert sorted(p.name for p in source.parent.iterdir()) == [
        "argus.db",
        "secrets.sops.yaml",
    ]


def test_seed_skips_existing_database_unless_forced(
    source: Path, tmp_path: Path
) -> None:
    out = tmp_path / ".argus"
    out.mkdir()
    (out / "argus.db").write_bytes(b"mine")
    assert not dev.seed(source, out)
    assert (out / "argus.db").read_bytes() == b"mine"
    assert dev.seed(source, out, force=True)


def test_oversize_or_missing_source_warns_and_starts_empty(
    source: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / ".argus"
    assert not dev.seed(source, out, size_limit=1)
    assert "over the" in capsys.readouterr().err
    assert not dev.seed(tmp_path / "none.db", out)
    assert "starting empty" in capsys.readouterr().err
    assert not (out / "argus.db").exists()


def test_seed_refuses_the_source_data_dir(source: Path) -> None:
    with pytest.raises(dev.DevError, match="source data dir"):
        dev.seed(source, source.parent, force=True)


def test_newer_schema_than_this_checkout_is_refused(
    source: Path, tmp_path: Path
) -> None:
    conn = sqlite3.connect(source)
    conn.execute(f"PRAGMA user_version = {len(load_migrations()) + 1}")
    conn.close()
    out = tmp_path / ".argus"
    with pytest.raises(dev.DevError, match="schema"):
        dev.seed(source, out)
    assert list(out.glob("argus.db*")) == []


def test_every_table_is_classified(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    for m in load_migrations():
        conn.executescript(m.sql)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'knowledge_fts%'"
        )
    }
    assert tables <= {*dev.DROP_TABLES, *dev.ACCOUNT_TABLES, *dev.KEEP_TABLES}


def test_ports_are_stable_in_range_and_shift_when_occupied() -> None:
    root = Path("/work/tree-a")
    first = dev.pick_port(root, dev.API_PORTS, lambda _: True)
    assert first == dev.pick_port(root, dev.API_PORTS, lambda _: True)
    assert dev.API_PORTS[0] <= first <= dev.API_PORTS[1]
    assert dev.pick_port(root, dev.API_PORTS, lambda p: p != first) == (
        first + 1 if first < dev.API_PORTS[1] else dev.API_PORTS[0]
    )
    skipped = {first, first + 1}
    assert dev.pick_port(root, dev.API_PORTS, lambda p: p not in skipped) != first
    other = {
        dev.pick_port(Path(f"/work/tree-{n}"), dev.DASHBOARD_PORTS, lambda _: True)
        for n in range(20)
    }
    assert len(other) > 1 and all(3100 <= p <= 3199 for p in other)
    with pytest.raises(dev.DevError):
        dev.pick_port(root, dev.API_PORTS, lambda _: False)


def test_dev_line_names_both_urls() -> None:
    assert dev.dev_line(8123, 3123) == (
        "dev: api=http://127.0.0.1:8123 dashboard=http://127.0.0.1:3123"
    )


def test_data_dir_defaults_to_checkout_and_ignores_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", "/somewhere/else")
    assert dev.resolve_data_dir(tmp_path, None) == tmp_path / ".argus"
    assert dev.resolve_data_dir(tmp_path, tmp_path / "x") == tmp_path / "x"
