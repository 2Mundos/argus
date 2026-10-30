from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse

from argus_api.access import (
    is_global_reader,
    require_artifact_readable,
    require_task_readable,
)
from argus_api.artifacts import inputs, service
from argus_api.artifacts.inputs import ReceiptRequest, TaskInput, TaskInputs
from argus_api.artifacts.service import (
    Artifact,
    ArtifactRole,
    ArtifactText,
    TaskArtifact,
    Verification,
)
from argus_api.artifacts.store import LocalArtifactStore
from argus_api.deps import AgentDep, CallerDep, DbDep, ManagerDep, SettingsDep

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
    settings: SettingsDep,
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
        agent_quota_bytes=settings.artifact_agent_quota_bytes,
    )


@router.post(
    "/tasks/{task_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {"content": {"application/octet-stream": {}}, "required": True}
    },
)
async def attach_artifact(
    task_id: str,
    request: Request,
    db: DbDep,
    store: StoreDep,
    caller: ManagerDep,
    settings: SettingsDep,
    name: Annotated[str, Query()],
    role: Annotated[ArtifactRole | None, Query()] = None,
    worker_readable: Annotated[bool, Query()] = False,
    expected_size: Annotated[int | None, Query(ge=0)] = None,
    expected_sha256: Annotated[str | None, Query(pattern=inputs.SHA256)] = None,
) -> Artifact:
    """Attach the raw request body to a task as evidence (orchestrator or admin).

    Needs no lease and does not count against any agent quota. Attachments have
    `source: attachment`, are immutable, and are readable by orchestrators and
    admins only, unless `worker_readable` shares them with every worker holding a
    lease grant on the task (then they are the task's inputs). `expected_size` and
    `expected_sha256` are checked against the received bytes before anything is
    stored: a difference is 422 `integrity_mismatch` and leaves no attachment.
    """
    return await service.attach(
        db,
        store,
        attached_by=f"{caller.scope}:{caller.agent_id or caller.label or caller.token_id[:8]}",
        task_id=task_id,
        name=name,
        media_type=service.check_media_type(
            request.headers.get("content-type", "application/octet-stream")
        ),
        chunks=request.stream(),
        role=role,
        max_per_task=settings.attachment_max_per_task,
        worker_readable=worker_readable,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )


@router.get("/tasks/{task_id}/inputs")
async def list_task_inputs(task_id: str, db: DbDep, caller: CallerDep) -> TaskInputs:
    """A task's inputs: attachments shared with workers, with the size and SHA-256
    Argus computed, provenance, and the latest receipt per attempt and state.
    Managers also get `unshared_attachments`, a count of attachments not shared."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        return await inputs.list_inputs(conn, task_id, manager=is_global_reader(caller))


@router.post("/tasks/{task_id}/inputs/{artifact_id}/receipt")
async def post_input_receipt(
    task_id: str,
    artifact_id: str,
    body: ReceiptRequest,
    db: DbDep,
    agent_id: AgentDep,
) -> TaskInput:
    """Record that the attempt you hold materialized, verified, consumed or failed
    an input. Fenced like progress; `sha256` and `size`, when given, must be the
    artifact's (422 `integrity_mismatch`). An identical latest receipt is not
    written again."""
    async with db.write() as tx:
        return await inputs.record_receipt(tx, agent_id, task_id, artifact_id, body)


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(
    task_id: str,
    db: DbDep,
    caller: CallerDep,
    attempt: int | None = None,
    role: ArtifactRole | None = None,
) -> list[TaskArtifact]:
    """A task's artifacts (metadata only), by attempt then name; `in_result` says
    whether the task's result lists one."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        return await service.list_artifacts(
            conn,
            task_id,
            attempt=attempt,
            role=role,
            include_attachments=is_global_reader(caller),
            include_shared=True,  # the task rule above already holds
        )


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, db: DbDep, caller: CallerDep) -> Artifact:
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
        await require_artifact_readable(
            conn, caller, artifact.task_id, artifact.source, artifact.worker_readable
        )
        return artifact


@router.get("/artifacts/{artifact_id}/content", response_class=FileResponse)
async def download_artifact(
    artifact_id: str, db: DbDep, store: StoreDep, caller: CallerDep
) -> FileResponse:
    """The raw bytes. Honours `Range: bytes=…` for partial reads."""
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
        await require_artifact_readable(
            conn, caller, artifact.task_id, artifact.source, artifact.worker_readable
        )
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
    caller: CallerDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    max_bytes: Annotated[int, Query(ge=64, le=65536)] = 16384,
) -> ArtifactText:
    """Up to `max_bytes` of a text or JSON artifact from `offset`, as UTF-8;
    continue from `next_offset`. Other media types return `binary` and no bytes."""
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
        await require_artifact_readable(
            conn, caller, artifact.task_id, artifact.source, artifact.worker_readable
        )
    return await service.read_text(store, artifact, offset, max_bytes)


@router.post("/artifacts/{artifact_id}/verify")
async def verify_artifact(
    artifact_id: str, db: DbDep, store: StoreDep, caller: CallerDep
) -> Verification:
    """Re-hash the stored bytes against the size and SHA-256 recorded at upload.
    `ok` means intact, not acceptable."""
    async with db.read() as conn:
        artifact = await service.get_artifact(conn, artifact_id)
        await require_artifact_readable(
            conn, caller, artifact.task_id, artifact.source, artifact.worker_readable
        )
    return await service.verify(store, artifact)
