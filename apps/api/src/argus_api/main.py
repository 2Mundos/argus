from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from argus_core.tasks import InvalidTransitionError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from argus_api.config import Settings
from argus_api.db import Database, DataDirLock
from argus_api.deps import get_db
from argus_api.errors import DomainError
from argus_api.tasks.routes import router as tasks_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with DataDirLock(settings.data_dir):
        db = await Database.open(settings.db_path)
        app.state.db = db
        try:
            yield
        finally:
            await db.close()


app = FastAPI(title="Argus", version="0.1.0", lifespan=lifespan)
app.include_router(tasks_router)


@app.exception_handler(DomainError)
async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(InvalidTransitionError)
async def invalid_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
    return _error(409, "invalid_transition", str(exc))


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status)


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    async with get_db(request).read() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}
