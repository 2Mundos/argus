from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse

from argus_api.artifacts import service
from argus_api.artifacts.service import (
    Artifact,
    ArtifactRole,
    ArtifactText,
    TaskArtifact,
    Verification,
)
from argus_api.artifacts.store import LocalArtifactStore
from argus_api.deps import AgentDep, CallerDep, DbDep

router = APIRouter(tags=["artifacts"])


def get_store(request: Request) -> LocalArtifactStore:
    return request.app.state.artifacts


StoreDep = Annotated[LocalArtifactStore, Depends(get_store)]


@router.post(
    "/tasks/{task_id}/artifacts",
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {"content": {"application/octet-stream": {}}, "required": True}
    },
)
async def upload_artifact(
    task_id: str,
    request: Request,
    db: DbDep,
    store: StoreDep,
    agent_id: AgentDep,
    attempt: Annotated[int, Query()],
    name: Annotated[str, Query()],
    role: Annotated[ArtifactRole | None, Query()] = None,
) -> Artifact:
    """Upload the raw request body as an artifact of the attempt you hold.

    List the returned id in `result.artifacts` when completing the task. `role`
    says what it is for (report, delegate_report, …); it is never inferred.
    """
    media_type = request.headers.get("content-type", "application/octet-stream")
    return await service.upload(
        db,
        store,
        agent_id=agent_id,
        task_id=task_id,
        attempt=attempt,
        name=name,
        media_type=media_type,
        chunks=request.stream(),
        role=role,
    )


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(
    task_id: str,
    db: DbDep,
    _: CallerDep,
    attempt: int | None = None,
    role: ArtifactRole | None = None,
) -> list[TaskArtifact]:
    """A task's artifacts (metadata only), by attempt then name; `in_result` says
    whether the task's result lists one."""
    async with db.read() as conn:
        return await service.list_artifacts(conn, task_id, attempt=attempt, role=role)


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, db: DbDep, _: CallerDep) -> Artifact:
    async with db.read() as conn:
        return await service.get_artifact(conn, artifact_id)


@router.get("/artifacts/{artifact_id}/content", response_class=FileResponse)
async def download_artifact(
    artifact_id: str, db: DbDep, store: StoreDep, _: CallerDep
) -> FileResponse:
    """The raw bytes. Honours `Range: bytes=…` for partial reads."""
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
    return FileResponse(
        store.path(service.key(artifact.task_id, artifact.attempt, artifact.name)),
        media_type=artifact.media_type,
        filename=artifact.name,
    )


@router.get("/artifacts/{artifact_id}/text")
async def read_artifact_text(
    artifact_id: str,
    db: DbDep,
    store: StoreDep,
    _: CallerDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    max_bytes: Annotated[int, Query(ge=64, le=65536)] = 16384,
) -> ArtifactText:
    """Up to `max_bytes` of a text or JSON artifact from `offset`, as UTF-8;
    continue from `next_offset`. Other media types return `binary` and no bytes."""
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
    return await service.read_text(store, artifact, offset, max_bytes)


@router.post("/artifacts/{artifact_id}/verify")
async def verify_artifact(
    artifact_id: str, db: DbDep, store: StoreDep, _: CallerDep
) -> Verification:
    """Re-hash the stored bytes against the size and SHA-256 recorded at upload.
    `ok` means intact, not acceptable."""
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
    return await service.verify(store, artifact)
