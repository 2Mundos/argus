import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from argus_core.tasks import InvalidTransitionError
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from argus_api import reaper
from argus_api.accounts import service as accounts
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
from argus_api.errors import DomainError
from argus_api.eventlog.routes import router as events_router
from argus_api.kinds.routes import router as kinds_router
from argus_api.knowledge.routes import router as knowledge_router
from argus_api.secrets import open_store
from argus_api.tasks.routes import router as tasks_router

log = logging.getLogger("argus")


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
                "Created the initial admin token (also saved to %s): %s",
                settings.data_dir / "admin-token",
                generated,
            )
        reaper_task = asyncio.create_task(reaper.run(db))
        try:
            yield
        finally:
            reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper_task
            await db.close()


app = FastAPI(title="Argus", version="0.1.0", lifespan=lifespan)

# The API lives under /api; the dashboard is served at / by the same process.
api = APIRouter(prefix="/api")
for router in (
    tasks_router,
    agents_router,
    admin_router,
    kinds_router,
    artifacts_router,
    decisions_router,
    events_router,
    knowledge_router,
    accounts_router,
):
    api.include_router(router)
app.include_router(api)


@app.exception_handler(DomainError)
async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message, exc.details)


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
