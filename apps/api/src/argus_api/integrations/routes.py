"""REST routes for integrations (docs/specs/integrations.spec.md 3.4, 3.6).

Records and task opt-in are admin only and never return a secret. The two worker
routes are internal (left out of the OpenAPI document): the non-secret status and
the one route that returns a credential, to the worker holding the attempt.
"""

from typing import Annotated

from argus_core.auth import Scope
from argus_core.tasks import Task
from fastapi import APIRouter, Query, Request, Response, status

from argus_api.auth import Identity
from argus_api.deps import AdminDep, DbDep, SecretsDep, require
from argus_api.integrations import delivery, service
from argus_api.integrations.delivery import (
    DeliveryRequest,
    IntegrationState,
    KubernetesCredential,
    TaskIntegrations,
)
from argus_api.integrations.service import (
    Integration,
    IntegrationCreate,
    IntegrationRotate,
)

router = APIRouter(tags=["integrations"])


def _probe(request: Request) -> dict:
    # Tests install a fake transport and resolver; production uses neither.
    return {
        "transport": getattr(request.app.state, "integration_transport", None),
        "resolve": getattr(request.app.state, "integration_resolver", None),
    }


@router.get("/integrations")
async def list_integrations(db: DbDep, _: AdminDep) -> list[Integration]:
    """Every project's integrations, revoked included. Never a secret."""
    return await service.list_all(db)


@router.get("/projects/{slug}/integrations")
async def list_project_integrations(
    slug: str, db: DbDep, _: AdminDep
) -> list[Integration]:
    return await service.list_all(db, slug)


@router.post("/projects/{slug}/integrations", status_code=status.HTTP_201_CREATED)
async def create_integration(
    slug: str,
    body: IntegrationCreate,
    request: Request,
    db: DbDep,
    caller: AdminDep,
    secrets: SecretsDep,
) -> Integration:
    """The token goes to the SOPS file; the row keeps only its reference."""
    return await service.create(
        db,
        secrets,
        slug,
        body,
        caller.actor,
        store_available=request.app.state.secrets_ok,
    )


@router.get("/projects/{slug}/integrations/{integration_id}")
async def get_integration(
    slug: str, integration_id: str, db: DbDep, _: AdminDep
) -> Integration:
    return await service.get(db, slug, integration_id)


@router.post("/projects/{slug}/integrations/{integration_id}/validate")
async def validate_integration(
    slug: str,
    integration_id: str,
    request: Request,
    db: DbDep,
    caller: AdminDep,
    secrets: SecretsDep,
) -> Integration:
    return await service.validate(
        db,
        secrets,
        slug,
        integration_id,
        caller.actor,
        store_available=request.app.state.secrets_ok,
        **_probe(request),
    )


@router.post("/projects/{slug}/integrations/{integration_id}/rotate")
async def rotate_integration(
    slug: str,
    integration_id: str,
    body: IntegrationRotate,
    request: Request,
    db: DbDep,
    caller: AdminDep,
    secrets: SecretsDep,
) -> Integration:
    """Replace the token; validation goes back to `unchecked`."""
    return await service.rotate(
        db,
        secrets,
        slug,
        integration_id,
        body,
        caller.actor,
        store_available=request.app.state.secrets_ok,
    )


@router.post("/projects/{slug}/integrations/{integration_id}/revoke")
async def revoke_integration(
    slug: str,
    integration_id: str,
    request: Request,
    db: DbDep,
    caller: AdminDep,
    secrets: SecretsDep,
) -> Integration:
    """Delete the token from the SOPS file; the record stays, revoked."""
    return await service.revoke(
        db,
        secrets,
        slug,
        integration_id,
        caller.actor,
        store_available=request.app.state.secrets_ok,
    )


@router.put("/tasks/{task_id}/integrations")
async def set_task_integrations(
    task_id: str, body: TaskIntegrations, db: DbDep, caller: AdminDep
) -> Task:
    """Opt a task into Kubernetes integrations of its own project (ids only), or
    clear the list. Each id must be active and of kind `kubernetes`; not while an
    attempt runs. Selecting is the only grant: capabilities, titles and pool
    labels never give a task a credential."""
    async with db.write() as tx:
        return await delivery.set_selection(tx, task_id, body, caller.actor)


# Worker only: an orchestrator or admin token holds no attempt and gets 403.
WorkerDep = Annotated[Identity, require(Scope.WORKER)]


@router.get("/tasks/{task_id}/integrations/status", include_in_schema=False)
async def task_integration_status(
    task_id: str,
    attempt: Annotated[int, Query()],
    request: Request,
    db: DbDep,
    caller: WorkerDep,
    secrets: SecretsDep,
) -> list[IntegrationState]:
    """Non-secret: each selected integration and whether it is deliverable now."""
    assert caller.agent_id is not None
    async with db.snapshot() as snap:
        return await delivery.status(
            snap,
            task_id,
            attempt,
            caller.agent_id,
            secrets,
            store_available=request.app.state.secrets_ok,
        )


@router.post(
    "/tasks/{task_id}/integrations/{integration_id}/credential",
    include_in_schema=False,
)
async def deliver_task_integration(
    task_id: str,
    integration_id: str,
    body: DeliveryRequest,
    request: Request,
    response: Response,
    db: DbDep,
    caller: WorkerDep,
    secrets: SecretsDep,
) -> KubernetesCredential:
    """The credential, to the worker holding this exact attempt (spec 3.6)."""
    assert caller.agent_id is not None
    response.headers["Cache-Control"] = "no-store"
    async with db.write() as tx:
        return await delivery.deliver(
            tx,
            task_id,
            integration_id,
            body,
            caller.agent_id,
            caller.actor,
            secrets,
            store_available=request.app.state.secrets_ok,
        )
