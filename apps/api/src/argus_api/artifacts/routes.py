from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse

from argus_api.artifacts import service
from argus_api.artifacts.service import Artifact
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
) -> Artifact:
    """Upload the raw request body as an artifact of the attempt you hold.

    List the returned id in `result.artifacts` when completing the task.
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
    )


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(task_id: str, db: DbDep, _: CallerDep) -> list[Artifact]:
    async with db.read() as conn:
        return await service.list_artifacts(conn, task_id)


@router.get("/artifacts/{artifact_id}/content", response_class=FileResponse)
async def download_artifact(
    artifact_id: str, db: DbDep, store: StoreDep, _: CallerDep
) -> FileResponse:
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
    return FileResponse(
        store.path(service.key(artifact.task_id, artifact.attempt, artifact.name)),
        media_type=artifact.media_type,
        filename=artifact.name,
    )
