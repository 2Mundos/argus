"""Health, readiness and metrics (docs/specs/operations.spec.md)."""

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from argus_core.agents import AgentState
from argus_core.fleet import PoolStatus
from argus_core.tasks import TaskState

from argus_api.db import Database, utcnow

# Baked into release images (docs/specs/releases.spec.md 3.5).
VERSION = os.environ.get("ARGUS_VERSION", "0.0.0+dev")
WRITE_PROBE_TIMEOUT_S = 5.0
MIN_FREE_BYTES = 64 * 1024 * 1024
MIN_STALE_WINDOW_S = 30.0

CheckStatus = Literal["ok", "degraded", "failing"]


@dataclass
class LoopStats:
    """In-memory record of one background loop (not persisted)."""

    name: str
    interval_s: float
    started_at: float = field(default_factory=time.monotonic)
    last_success_unix: float | None = None
    last_success_mono: float | None = None
    last_duration_s: float | None = None
    errors_total: int = 0
    passes_total: int = 0

    def record(self, duration_s: float, ok: bool = True) -> None:
        self.passes_total += 1
        if ok:
            self.last_success_unix = time.time()
            self.last_success_mono = time.monotonic()
            self.last_duration_s = duration_s
        else:
            self.errors_total += 1

    @property
    def stale_after_s(self) -> float:
        return max(3 * self.interval_s, MIN_STALE_WINDOW_S)

    def status(self) -> CheckStatus:
        # Before the first pass, allow one stale window from loop start.
        reference = (
            self.last_success_mono
            if self.last_success_mono is not None
            else self.started_at
        )
        return "ok" if time.monotonic() - reference <= self.stale_after_s else "failing"


@dataclass
class Check:
    status: CheckStatus
    detail: str = ""


@dataclass
class Readiness:
    checks: dict[str, Check]

    @property
    def ready(self) -> bool:
        return all(c.status != "failing" for c in self.checks.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "unready",
            "checks": {
                k: {"status": c.status, **({"detail": c.detail} if c.detail else {})}
                for k, c in self.checks.items()
            },
        }


async def readiness(
    db: Database,
    loops: dict[str, LoopStats],
    data_dir: Path,
    secrets_ok: bool,
) -> Readiness:
    checks: dict[str, Check] = {}
    try:
        async with asyncio.timeout(WRITE_PROBE_TIMEOUT_S):
            async with db.write():  # an empty transaction: proves the writer is free
                pass
        checks["database"] = Check("ok")
    except TimeoutError:
        checks["database"] = Check(
            "failing", f"no write slot within {WRITE_PROBE_TIMEOUT_S:g} s"
        )
    except Exception as exc:
        checks["database"] = Check("failing", type(exc).__name__)
    for name, stats in loops.items():
        s = stats.status()
        checks[name] = Check(
            s, "" if s == "ok" else f"no pass within {stats.stale_after_s:g} s"
        )
    free = disk_free(data_dir)
    checks["disk"] = Check(
        "ok" if free >= MIN_FREE_BYTES else "failing",
        "" if free >= MIN_FREE_BYTES else f"{free // (1024 * 1024)} MiB free",
    )
    checks["secrets"] = Check(
        "ok" if secrets_ok else "degraded", "" if secrets_ok else "store not loaded"
    )
    return Readiness(checks)


def disk_free(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


@dataclass
class Snapshot:
    """Everything /metrics and /api/system report, gathered in one pass."""

    tasks: dict[str, int]
    oldest_ready_age_s: float
    agents: dict[str, int]
    instances: dict[tuple[str, str], int]
    pools: list[dict[str, Any]]
    accounts: dict[tuple[str, str], int]
    # (provider, account name) -> input, output, cached tokens ever reported
    account_tokens: dict[tuple[str, str], tuple[int, int, int]]
    # (provider, account name, window) -> latest % used
    account_quota: dict[tuple[str, str, str], float]
    decisions_open: int
    events_total: int
    db_main_bytes: int
    db_wal_bytes: int
    disk_free_bytes: int


async def snapshot(
    db: Database, db_path: Path, data_dir: Path, now: datetime | None = None
) -> Snapshot:
    now = now or utcnow()
    async with db.read() as conn:

        async def one(sql: str, *params: Any) -> Any:
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
            return row[0] if row else None

        async def grouped(sql: str) -> list[tuple[Any, ...]]:
            async with conn.execute(sql) as cur:
                return [tuple(r) for r in await cur.fetchall()]

        tasks = {s.value: 0 for s in TaskState} | dict(
            await grouped(
                "SELECT state, count(*) FROM tasks"
                " WHERE archived_at IS NULL GROUP BY state"
            )
        )
        oldest = await one("SELECT min(created_at) FROM tasks WHERE state = 'ready'")
        age = (now - datetime.fromisoformat(oldest)).total_seconds() if oldest else 0.0
        agents = {s.value: 0 for s in AgentState} | dict(
            await grouped("SELECT state, count(*) FROM agents GROUP BY state")
        )
        instances: dict[tuple[str, str], int] = {}
        for pool, state, n in await grouped(
            "SELECT pool, state, count(*) FROM instances GROUP BY pool, state"
        ):
            instances[(pool, state)] = n
        pools = [
            {"name": r[0], "desired": r[1], "max": r[2], "status": r[3], "live": r[4]}
            for r in await grouped(
                "SELECT p.name, p.desired_replicas, p.max_replicas, p.status,"
                " (SELECT count(*) FROM instances i WHERE i.pool = p.name"
                "  AND i.state != 'terminated') FROM pools p ORDER BY p.name"
            )
        ]
        accounts = {
            (p, s): n
            for p, s, n in await grouped(
                "SELECT provider, state, count(*) FROM accounts GROUP BY provider, state"
            )
        }
        account_tokens = {
            (p, name): (i, o, c)
            for p, name, i, o, c in await grouped(
                "SELECT a.provider, a.name, coalesce(sum(u.input_tokens), 0),"
                " coalesce(sum(u.output_tokens), 0),"
                " coalesce(sum(u.cached_input_tokens), 0)"
                " FROM accounts a JOIN account_usage u ON u.account_id = a.id"
                " GROUP BY a.id"
            )
        }
        account_quota = {
            (p, name, window): pct
            for p, name, window, pct in await grouped(
                "SELECT a.provider, a.name, q.window, q.used_percent"
                " FROM account_quota q JOIN accounts a ON a.id = q.account_id"
                " WHERE q.used_percent IS NOT NULL"
            )
        }
        decisions_open = (
            await one("SELECT count(*) FROM decisions WHERE state = 'open'") or 0
        )
        events_total = await one("SELECT coalesce(max(id), 0) FROM events") or 0
    wal = Path(f"{db_path}-wal")
    return Snapshot(
        tasks=tasks,
        oldest_ready_age_s=max(age, 0.0),
        agents=agents,
        instances=instances,
        pools=pools,
        accounts=accounts,
        account_tokens=account_tokens,
        account_quota=account_quota,
        decisions_open=decisions_open,
        events_total=events_total,
        db_main_bytes=_size(db_path),
        db_wal_bytes=_size(wal),
        disk_free_bytes=disk_free(data_dir),
    )


def _size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def render_metrics(
    snap: Snapshot,
    loops: dict[str, LoopStats],
    *,
    uptime_s: float,
    secrets_ok: bool,
) -> str:
    out: list[str] = []

    def family(name: str, help_: str, kind: str = "gauge") -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")

    def sample(name: str, value: float | int, **labels: str) -> None:
        lab = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        out.append(f"{name}{{{lab}}} {_num(value)}" if lab else f"{name} {_num(value)}")

    family("argus_build_info", "Build information.")
    sample("argus_build_info", 1, version=VERSION)
    family("argus_uptime_seconds", "Process uptime.")
    sample("argus_uptime_seconds", uptime_s)
    family("argus_tasks", "Tasks per state.")
    for state, n in sorted(snap.tasks.items()):
        sample("argus_tasks", n, state=state)
    family(
        "argus_task_oldest_ready_age_seconds",
        "Age of the oldest ready task (0 if none).",
    )
    sample("argus_task_oldest_ready_age_seconds", snap.oldest_ready_age_s)
    family("argus_agents", "Agents per state.")
    for state, n in sorted(snap.agents.items()):
        sample("argus_agents", n, state=state)
    family("argus_instances", "Instances per pool and state.")
    for (pool, state), n in sorted(snap.instances.items()):
        sample("argus_instances", n, pool=pool, state=state)
    family("argus_pool_replicas", "Desired, maximum and live replicas per pool.")
    for p in snap.pools:
        for kind, key in (("desired", "desired"), ("max", "max"), ("live", "live")):
            sample("argus_pool_replicas", p[key], pool=p["name"], kind=kind)
    family("argus_pool_status", "1 for each pool's current status.")
    for p in snap.pools:
        for status in PoolStatus:
            sample(
                "argus_pool_status",
                int(p["status"] == status.value),
                pool=p["name"],
                status=status.value,
            )
    family("argus_accounts", "Provider accounts per provider and state.")
    for (provider, state), n in sorted(snap.accounts.items()):
        sample("argus_accounts", n, provider=provider, state=state)
    family("argus_account_tokens_total", "Tokens reported per account.", "counter")
    for (provider, name), counts in sorted(snap.account_tokens.items()):
        for kind, n in zip(("input", "output", "cached"), counts, strict=True):
            sample(
                "argus_account_tokens_total",
                n,
                provider=provider,
                account=name,
                kind=kind,
            )
    family(
        "argus_account_quota_used_percent",
        "Latest provider-reported percent used per quota window.",
    )
    for (provider, name, window), pct in sorted(snap.account_quota.items()):
        sample(
            "argus_account_quota_used_percent",
            pct,
            provider=provider,
            account=name,
            window=window,
        )
    family("argus_decisions_open", "Decisions waiting for an answer.")
    sample("argus_decisions_open", snap.decisions_open)
    family("argus_events_total", "Rows ever written to the event log.", "counter")
    sample("argus_events_total", snap.events_total)
    family(
        "argus_loop_last_success_timestamp_seconds",
        "Unix time of a loop's last good pass.",
    )
    ordered = sorted(loops.items())
    for name, s in ordered:
        if s.last_success_unix is not None:
            sample(
                "argus_loop_last_success_timestamp_seconds",
                s.last_success_unix,
                loop=name,
            )
    family("argus_loop_duration_seconds", "Duration of a loop's last good pass.")
    for name, s in ordered:
        if s.last_duration_s is not None:
            sample("argus_loop_duration_seconds", s.last_duration_s, loop=name)
    family("argus_loop_errors_total", "Failed loop passes.", "counter")
    for name, s in ordered:
        sample("argus_loop_errors_total", s.errors_total, loop=name)
    family("argus_secret_store_up", "1 if the secret store loaded.")
    sample("argus_secret_store_up", int(secrets_ok))
    family("argus_database_size_bytes", "SQLite file sizes.")
    sample("argus_database_size_bytes", snap.db_main_bytes, file="main")
    sample("argus_database_size_bytes", snap.db_wal_bytes, file="wal")
    family("argus_disk_free_bytes", "Free space on the data directory's filesystem.")
    sample("argus_disk_free_bytes", snap.disk_free_bytes)
    return "\n".join(out) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(value: float | int) -> str:
    return (
        str(int(value)) if float(value).is_integer() else repr(round(float(value), 6))
    )
