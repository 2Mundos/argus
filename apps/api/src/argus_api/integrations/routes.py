"""REST routes for integrations (docs/specs/integrations.spec.md 3.4, 3.6).

Admin only. No route returns a secret: workers do not receive integrations
(spec 3.6 says why).
"""

from fastapi import APIRouter, Request, status

from argus_api.deps import AdminDep, DbDep, SecretsDep
from argus_api.integrations import service
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
