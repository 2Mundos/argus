from fastapi import FastAPI

app = FastAPI(title="Argus", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
