from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from argus_api.config import Settings
from argus_api.db import Database, DataDirLock


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


def get_db(request: Request) -> Database:
    return request.app.state.db


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    async with get_db(request).read() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}
