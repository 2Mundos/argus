"""Health, readiness, metrics and the system summary (operations spec)."""

import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from argus_api import ops
from argus_api.deps import AdminDep, CallerDep, DbDep, SettingsDep

# Probes sit outside /api (like /healthz); the rest live under it.
probes = APIRouter(tags=["operations"])
router = APIRouter(tags=["operations"])


def _loops(request: Request) -> dict[str, ops.LoopStats]:
    return request.app.state.loops


async def _readiness(
    request: Request, db: DbDep, settings: SettingsDep
) -> ops.Readiness:
    return await ops.readiness(
        db, _loops(request), settings.data_dir, request.app.state.secrets_ok
    )


@probes.get("/readyz", include_in_schema=False)
async def readyz(request: Request, db: DbDep, settings: SettingsDep) -> Response:
    """Can Argus do its job? 503 only when a check is failing."""
    r = await _readiness(request, db, settings)
    return JSONResponse(r.as_dict(), status_code=200 if r.ready else 503)


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(
    request: Request, db: DbDep, settings: SettingsDep, _: AdminDep
) -> Response:
    """Prometheus text format; needs an admin token (`authorization` in the scrape config)."""
    snap = await ops.snapshot(db, settings.db_path, settings.data_dir)
    text = ops.render_metrics(
        snap,
        _loops(request),
        uptime_s=time.monotonic() - request.app.state.started_at,
        secrets_ok=request.app.state.secrets_ok,
    )
    return PlainTextResponse(
        text, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


class CheckOut(BaseModel):
    status: str
    detail: str | None = None


class ReadinessOut(BaseModel):
    status: str
    checks: dict[str, CheckOut]


class LoopOut(BaseModel):
    status: str
    last_success_age_s: float | None
    last_duration_s: float | None
    errors_total: int
    passes_total: int


class PoolOut(BaseModel):
    name: str
    desired: int
    max: int
    live: int
    status: str


class AccountCount(BaseModel):
    provider: str
    state: str
    count: int


class DatabaseOut(BaseModel):
    main_bytes: int
    wal_bytes: int


class SystemSummary(BaseModel):
    version: str
    uptime_s: float
    readiness: ReadinessOut
    tasks: dict[str, int]
    oldest_ready_age_s: float
    agents: dict[str, int]
    pools: list[PoolOut]
    accounts: list[AccountCount]
    decisions_open: int
    events_total: int
    database: DatabaseOut
    disk_free_bytes: int
    loops: dict[str, LoopOut]


@router.get("/system")
async def system(
    request: Request, db: DbDep, settings: SettingsDep, _: CallerDep
) -> SystemSummary:
    """Readiness plus the numbers /metrics reports, as JSON for the dashboard."""
    r = await _readiness(request, db, settings)
    snap = await ops.snapshot(db, settings.db_path, settings.data_dir)
    now = time.monotonic()
    return SystemSummary(
        version=ops.VERSION,
        uptime_s=now - request.app.state.started_at,
        readiness=ReadinessOut.model_validate(r.as_dict()),
        tasks=snap.tasks,
        oldest_ready_age_s=snap.oldest_ready_age_s,
        agents=snap.agents,
        pools=[PoolOut.model_validate(p) for p in snap.pools],
        accounts=[
            AccountCount(provider=p, state=s, count=n)
            for (p, s), n in sorted(snap.accounts.items())
        ],
        decisions_open=snap.decisions_open,
        events_total=snap.events_total,
        database=DatabaseOut(
            main_bytes=snap.db_main_bytes, wal_bytes=snap.db_wal_bytes
        ),
        disk_free_bytes=snap.disk_free_bytes,
        loops={
            name: LoopOut(
                status=st.status(),
                last_success_age_s=(
                    now - st.last_success_mono if st.last_success_mono else None
                ),
                last_duration_s=st.last_duration_s,
                errors_total=st.errors_total,
                passes_total=st.passes_total,
            )
            for name, st in _loops(request).items()
        },
    )
