import asyncio
import contextlib
import hashlib
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from argus_core.tasks import InvalidTransitionError
from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import Scope

from argus_api import mcp_server, reaper
from argus_api.accounts import service as accounts
from argus_api.accounts.claude_login import ClaudeLoginManager
from argus_api.accounts.codex_login import CodexLoginManager
from argus_api.accounts.quota_probe import QuotaProber
from argus_api.accounts.routes import router as accounts_router
from argus_api.admin.routes import router as admin_router
from argus_api.agents.routes import router as agents_router
from argus_api.artifacts.routes import router as artifacts_router
from argus_api.artifacts.store import LocalArtifactStore
from argus_api.auth import ensure_admin_token
from argus_api.config import Settings
from argus_api.db import Database, DataDirLock
from argus_api.decisions.routes import router as decisions_router
from argus_api.deps import get_db
from argus_api.engines.local import LocalModel
from argus_api.engines.routes import router as engines_router
from argus_api.errors import DomainError
from argus_api.eventlog.routes import router as events_router
from argus_api.fleet import reconciler
from argus_api.fleet.reconciler import FleetSettings
from argus_api.fleet.routes import router as fleet_router
from argus_api.fleet.targets import TargetRegistry
from argus_api.kinds.routes import router as kinds_router
from argus_api.knowledge.routes import router as knowledge_router
from argus_api.model_policy import router as models_router
from argus_api.modeltrace.routes import router as model_trace_router
from argus_api.ops import LoopStats, ReadinessCache
from argus_api.ops_routes import probes as probes_router
from argus_api.ops_routes import router as ops_router
from argus_api.runtime.docker import CliDocker
from argus_api.runtime.routes import router as runtime_router
from argus_api.runtime.service import RuntimeManager
from argus_api.secrets import open_store
from argus_api.tasks.delivery import PrChecker
from argus_api.tasks.routes import router as tasks_router

log = logging.getLogger("argus")

# HTML CSP for the dashboard; API/JSON responses only get frame-ancestors.
_DASHBOARD_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "object-src 'none'; base-uri 'none'"
)
_API_CSP = "frame-ancestors 'none'"


def _api_docs_enabled() -> bool:
    return os.environ.get("ARGUS_API_DOCS") == "1"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline browser hardening on every response (security-hardening spec)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        ct = response.headers.get("content-type", "")
        csp = _DASHBOARD_CSP if "text/html" in ct else _API_CSP
        response.headers.setdefault("Content-Security-Policy", csp)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with DataDirLock(settings.data_dir):
        db = await Database.open(settings.db_path)
        app.state.db = db
        app.state.settings = settings
        app.state.artifacts = LocalArtifactStore(
            settings.artifacts_dir, settings.artifact_max_bytes
        )
        secrets, secrets_error = await open_store(
            settings.secrets_path, settings.sops_bin
        )
        if secrets_error:
            log.error("secret store unavailable: %s", secrets_error)
        app.state.secrets = secrets
        try:
            async with db.write() as tx:
                generated = await ensure_admin_token(
                    tx, settings.data_dir, settings.admin_token
                )
        except BaseException:
            await db.close()
            raise
        async with db.write() as tx:
            await accounts.reconcile_secrets(tx, secrets)
        if generated:
            log.warning(
                "Created the initial admin token; read it from %s",
                settings.data_dir / "admin-token",
            )
        app.state.local_model = (
            LocalModel(
                settings.local_model_url,
                settings.local_model,
                timeout_s=settings.local_timeout_s,
            )
            if settings.local_model_url and settings.local_model
            else None
        )
        app.state.codex_login = CodexLoginManager(db, secrets, settings)
        app.state.claude_login = ClaudeLoginManager(db, secrets, settings)
        app.state.targets = TargetRegistry()
        app.state.fleet = FleetSettings(
            argus_url=settings.public_url,
            model_policy_path=settings.model_policy_path,
        )
        app.state.secrets_ok = secrets_error is None
        app.state.started_at = time.monotonic()
        app.state.readiness = ReadinessCache()
        app.state.quota = QuotaProber(db, secrets, settings)
        app.state.pr_checker = PrChecker(secrets)
        app.state.runtime = RuntimeManager(
            db,
            CliDocker(
                hashlib.sha256(str(settings.data_dir.resolve()).encode()).hexdigest()[
                    :12
                ]
            ),
            settings,
        )
        app.state.loops = {
            "reaper": LoopStats("reaper", reaper.REAP_INTERVAL_S),
            "reconciler": LoopStats("reconciler", settings.reconcile_interval_s),
        }
        local_model = app.state.local_model

        async def local_probe() -> tuple[bool | None, str]:
            """What the reaper tells the account registry about the local model:
            `local` accounts are available exactly while it answers."""
            if local_model is None:
                return None, ""
            info = await local_model.probe()
            ok = info.reachable and info.model == local_model.model
            return ok, info.error or f"serves {info.model}"

        loops = [
            asyncio.create_task(
                reaper.run(
                    db,
                    stats=app.state.loops["reaper"],
                    local_probe=local_probe if local_model else None,
                    quiet_claim_s=settings.quiet_claim_s,
                )
            ),
            asyncio.create_task(app.state.quota.run()),
            *(
                [asyncio.create_task(app.state.runtime.run())]
                if settings.project_runtime
                else []
            ),
            asyncio.create_task(
                reconciler.run(
                    db,
                    app.state.targets,
                    app.state.fleet,
                    settings.reconcile_interval_s,
                    app.state.loops["reconciler"],
                )
            ),
        ]
        try:
            async with mcp_server.endpoint.running():
                yield
        finally:
            await app.state.codex_login.shutdown()
            await app.state.claude_login.shutdown()
            for loop in loops:
                loop.cancel()
            for loop in loops:
                with contextlib.suppress(asyncio.CancelledError):
                    await loop
            await db.close()


_docs = _api_docs_enabled()
app = FastAPI(
    title="Argus",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

# The API lives under /api; the dashboard is served at / by the same process.
api = APIRouter(prefix="/api")
for router in (
    tasks_router,
    model_trace_router,
    agents_router,
    admin_router,
    kinds_router,
    artifacts_router,
    decisions_router,
    events_router,
    knowledge_router,
    accounts_router,
    fleet_router,
    engines_router,
    models_router,
    ops_router,
    runtime_router,
):
    api.include_router(router)
app.include_router(api)
app.include_router(probes_router)

mcp_server.bind(app)
app.mount("/mcp", mcp_server.endpoint)
app.add_middleware(mcp_server.SlashAlias)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(DomainError)
async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def request_invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422, without the offending input: a body can hold a secret."""
    errors = [
        {k: v for k, v in e.items() if k not in ("input", "ctx", "url")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.exception_handler(InvalidTransitionError)
async def invalid_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
    return _error(409, "invalid_transition", str(exc))


def _error(
    status: int, code: str, message: str, details: list[str] | None = None
) -> JSONResponse:
    body: dict[str, object] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse({"detail": body}, status)


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    async with get_db(request).read() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


# Mounted last: it matches every path the API did not. Unset in development,
# where Vite serves the dashboard and proxies /api.
class DashboardFiles(StaticFiles):
    """index.html must be revalidated on every load so an upgrade takes effect at
    once; Vite's content-hashed /assets/* never change, so they cache forever."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if scope["path"].startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


if (dashboard := Settings.from_env().dashboard_dir) is not None:
    app.mount("/", DashboardFiles(directory=dashboard, html=True), name="dashboard")
