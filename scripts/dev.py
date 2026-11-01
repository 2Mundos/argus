#!/usr/bin/env python3
"""Isolated dev servers per checkout.

Spec: docs/drafts/2026-10-31.worktree-dev-isolation.draft.md.

    dev.py api [--data-dir D] [--no-seed] [--accounts] [--keep-tasks N] [--source DB]
    dev.py dashboard
    dev.py seed [--accounts] [--keep-tasks N] [--source DB] [--data-dir D]

State lives in `<checkout>/.argus/` unless `--data-dir` says otherwise; an inherited
ARGUS_DATA_DIR is ignored. Ports derive from a hash of the checkout path. The first
`api` run on an empty data dir seeds it from the preview database: a read-only
backup, pruned so the copy cannot start workers or spend account quota. The source
is never written. Python code is only `argus_api`'s migration loader plus the
standard library.
"""

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

API_PORTS = (8100, 8199)
DASHBOARD_PORTS = (3100, 3199)
DEFAULT_SOURCE = Path.home() / "work" / "argus" / "data" / "argus.db"
SIZE_LIMIT = 512 * 1024 * 1024  # a larger source warns and starts empty
KEEP_TASKS = 200
KEEP_EVENTS = 10_000

# Task states that are queued work: a dev server must never find a claimable task.
QUEUED_STATES = ("pending", "ready", "claimed")
# Tables emptied outright: schedules, tokens, live fleet state and runtime hosts, plus
# artifact rows (bodies are not copied, so a row would point at nothing).
DROP_TABLES = (
    "routines",
    "tokens",
    "account_leases",
    "instances",
    "runtime_hosts",
    "runtime_builds",
    "workspace_cleanup",
    "workspace_cleanup_runs",
    "artifacts",
)
# Dropped unless `--accounts`; a child must be listed before its parent.
ACCOUNT_TABLES = (
    "account_credit",
    "account_quota",
    "account_quota_probe",
    "account_usage",
    "accounts",
)
# Rows kept as they are, apart from the task scoping done for every table that
# references tasks. A table missing from every list fails the seed, so a new one
# gets a decision when it is added (test_every_table_is_classified).
KEEP_TABLES = (
    "agents",
    "conversation_usage",
    "conversations",
    "decisions",
    "events",
    "knowledge_pages",
    "knowledge_proposals",
    "knowledge_revisions",
    "messages",
    "model_executions",
    "model_selections",
    "pools",
    "project_runtimes",
    "projects",
    "settings",
    "task_attempts",
    "task_changes",
    "task_deps",
    "task_draft_publications",
    "task_drafts",
    "task_kinds",
    "task_knowledge",
    "task_lease_grants",
    "task_log",
    "task_runtimes",
    "task_steers",
    "tasks",
)


class DevError(Exception):
    pass


def stable_index(root: Path, span: int) -> int:
    return int.from_bytes(hashlib.sha256(str(root).encode()).digest()[:8]) % span


def pick_port(
    root: Path,
    low_high: tuple[int, int],
    is_free: Callable[[int], bool],
    skip: frozenset[int] = frozenset(),
) -> int:
    """The hashed port of `root`, or the next free one in the range (wrapping)."""
    low, high = low_high
    span = high - low + 1
    start = stable_index(root, span)
    for step in range(span):
        port = low + (start + step) % span
        if port not in skip and is_free(port):
            return port
    raise DevError(f"no free port in {low}-{high}")


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def checkout_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip()).resolve()


def dev_line(api: int, dashboard: int) -> str:
    return f"dev: api=http://127.0.0.1:{api} dashboard=http://127.0.0.1:{dashboard}"


def _ports_file(root: Path) -> Path:
    return root / ".argus" / "dev-ports.json"


def choose_ports(
    root: Path, is_free: Callable[[int], bool] = port_is_free
) -> tuple[int, int]:
    return (
        pick_port(root, API_PORTS, is_free),
        pick_port(root, DASHBOARD_PORTS, is_free),
    )


# --- seeding ---------------------------------------------------------------


def _task_references(
    conn: sqlite3.Connection, tables: list[str]
) -> list[tuple[str, str]]:
    """(table, column) pairs that point at a task: a foreign key or a `task_id`."""
    found: set[tuple[str, str]] = set()
    for table in tables:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            if fk[2] == "tasks":
                found.add((table, fk[3]))
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        if "task_id" in cols:
            found.add((table, "task_id"))
    return sorted(found)


def prune(
    conn: sqlite3.Connection,
    *,
    keep_tasks: int = KEEP_TASKS,
    accounts: bool = False,
    now: datetime | None = None,
) -> None:
    """Reduce a copy of the preview database to a safe, realistic dev database.

    Driven by the copy's own schema, so a column or table added later is covered
    unless it is new: an unclassified table raises.
    """
    from argus_api.db.migrate import load_migrations

    latest = len(load_migrations())
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > latest:
        raise DevError(f"source is at schema {version}, this checkout knows {latest}")
    stamp = (now or datetime.now(UTC)).isoformat()
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'knowledge_fts%'"
        )
    ]
    known = {*DROP_TABLES, *ACCOUNT_TABLES, *KEEP_TABLES}
    if unknown := sorted(set(tables) - known):
        names = ", ".join(unknown)
        raise DevError(f"scripts/dev.py does not classify table(s): {names}")
    present = set(tables)

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    for table in DROP_TABLES + (() if accounts else ACCOUNT_TABLES):
        if table in present:
            conn.execute(f'DELETE FROM "{table}"')

    marks = ",".join("?" * len(QUEUED_STATES))
    conn.execute("CREATE TEMP TABLE keep_tasks (id TEXT PRIMARY KEY)")
    conn.execute(
        "INSERT INTO keep_tasks SELECT id FROM tasks"
        f" WHERE state NOT IN ({marks})"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (*QUEUED_STATES, keep_tasks),
    )
    outside = "NOT IN (SELECT id FROM keep_tasks)"
    for table, col in _task_references(conn, tables):
        if table == "tasks" or table not in present:
            continue
        notnull = any(
            r[1] == col and r[3] for r in conn.execute(f'PRAGMA table_info("{table}")')
        )
        if notnull:
            conn.execute(f'DELETE FROM "{table}" WHERE "{col}" {outside}')
        else:
            conn.execute(
                f'UPDATE "{table}" SET "{col}" = NULL'
                f' WHERE "{col}" IS NOT NULL AND "{col}" {outside}'
            )
    conn.execute(f"UPDATE tasks SET parent_id = NULL WHERE parent_id {outside}")
    conn.execute(f"DELETE FROM tasks WHERE id {outside}")
    conn.execute(
        f"DELETE FROM decisions WHERE (subject_type = 'task' AND subject_id {outside})"
        " OR (state = 'open' AND subject_type != 'task')"
    )
    conn.execute(
        f"DELETE FROM events WHERE subject_type = 'task' AND subject_id {outside}"
    )
    conn.execute(
        "DELETE FROM events WHERE id NOT IN"
        " (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
        (KEEP_EVENTS,),
    )
    conn.execute("DELETE FROM messages WHERE state = 'queued'")
    conn.execute("UPDATE conversations SET session_ref = NULL")
    conn.execute(
        "UPDATE agents SET state = 'gone', ended_at = ?, end_reason = 'dev seed'"
        " WHERE ended_at IS NULL",
        (stamp,),
    )
    conn.execute("UPDATE pools SET desired_replicas = 0, max_replicas = 0")
    conn.execute("COMMIT")


def seed(
    source: Path,
    data_dir: Path,
    *,
    keep_tasks: int = KEEP_TASKS,
    accounts: bool = False,
    size_limit: int = SIZE_LIMIT,
    force: bool = False,
) -> bool:
    """Create `data_dir/argus.db` from `source`. False when nothing was copied."""
    target = data_dir / "argus.db"
    if target.exists() and not force:
        return False
    if data_dir.resolve() == source.resolve().parent:
        raise DevError(f"refusing to seed into the source data dir {data_dir}")
    if not source.is_file():
        print(
            f"warning: no preview database at {source}; starting empty",
            file=sys.stderr,
        )
        return False
    size = sum(p.stat().st_size for p in source.parent.glob(source.name + "*"))
    if size > size_limit:
        print(
            f"warning: {source} is {size >> 20} MiB, over the {size_limit >> 20} MiB"
            " seed limit; starting empty",
            file=sys.stderr,
        )
        return False
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / "argus.db.seeding"
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{quote(str(source.resolve()))}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")
        prune(dst, keep_tasks=keep_tasks, accounts=accounts)
        dst.execute("VACUUM")
    except BaseException:
        dst.close()
        tmp.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    dst.close()
    tmp.chmod(0o600)
    for suffix in ("-wal", "-shm"):  # of a database this replaces
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    os.replace(tmp, target)
    if accounts:
        secrets = source.parent / "secrets.sops.yaml"
        if secrets.is_file():
            shutil.copy2(secrets, data_dir / "secrets.sops.yaml")
            (data_dir / "secrets.sops.yaml").chmod(0o600)
    print(f"seeded {target} from {source} (copy; the source is untouched)")
    return True


# --- commands --------------------------------------------------------------


def _seed_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--keep-tasks", type=int, default=KEEP_TASKS)
    p.add_argument("--accounts", action="store_true")


def resolve_data_dir(root: Path, explicit: Path | None) -> Path:
    return (explicit or root / ".argus").resolve()


def cmd_api(args: argparse.Namespace) -> int:
    root = checkout_root()
    data_dir = resolve_data_dir(root, args.data_dir)
    if not args.no_seed and not (data_dir / "argus.db").exists():
        seed(args.source, data_dir, keep_tasks=args.keep_tasks, accounts=args.accounts)
    api, dashboard = choose_ports(root)
    _ports_file(root).parent.mkdir(parents=True, exist_ok=True)
    _ports_file(root).write_text(json.dumps({"api": api, "dashboard": dashboard}))
    print(dev_line(api, dashboard), flush=True)
    env = {**os.environ, "ARGUS_DATA_DIR": str(data_dir)}
    os.execvpe(
        "uv",
        ["uv", "run", "uvicorn", "argus_api.main:app", "--reload", "--port", str(api)],
        env,
    )


def cmd_dashboard(_: argparse.Namespace) -> int:
    root = checkout_root()
    api, dashboard = choose_ports(root)
    try:
        recorded = json.loads(_ports_file(root).read_text())
        api = int(recorded["api"])
    except (OSError, ValueError, KeyError):
        pass  # no dev-api has run here: the hashed API port
    print(dev_line(api, dashboard), flush=True)
    env = {**os.environ, "ARGUS_API_URL": f"http://127.0.0.1:{api}"}
    os.execvpe(
        "pnpm",
        [
            "pnpm",
            "--filter",
            "@argus/dashboard",
            "exec",
            "vite",
            "--port",
            str(dashboard),
            "--strictPort",
        ],
        env,
    )


def cmd_seed(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(checkout_root(), args.data_dir)
    seed(
        args.source,
        data_dir,
        keep_tasks=args.keep_tasks,
        accounts=args.accounts,
        force=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("api")
    api.add_argument("--data-dir", type=Path)
    api.add_argument("--no-seed", action="store_true")
    _seed_args(api)
    api.set_defaults(run=cmd_api)
    sub.add_parser("dashboard").set_defaults(run=cmd_dashboard)
    seed_p = sub.add_parser("seed")
    seed_p.add_argument("--data-dir", type=Path)
    _seed_args(seed_p)
    seed_p.set_defaults(run=cmd_seed)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except DevError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
