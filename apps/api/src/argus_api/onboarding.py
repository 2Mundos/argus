"""Fresh-install onboarding progress (docs/specs/onboarding.spec.md)."""

from argus_core.accounts import ENGINE_PROVIDERS
from fastapi import APIRouter
from pydantic import BaseModel

from argus_api.deps import AdminDep, DbDep, SettingsDep
from argus_api.settings import service

router = APIRouter(tags=["onboarding"])

ONBOARDING_DISMISSED = "onboarding.dismissed"
ORCHESTRATOR_ENABLED = "orchestrator.enabled"


class OnboardingStatus(BaseModel):
    account: bool
    worker: bool
    orchestrator: bool
    dismissed: bool


async def status(conn, path) -> OnboardingStatus:
    """Derive wizard progress from accounts, pools and settings."""
    async with conn.execute("SELECT provider FROM accounts") as cur:
        account = any(row[0] in ENGINE_PROVIDERS for row in await cur.fetchall())

    # System pools (the internal orchestrator) do not count as workers.
    async with conn.execute("SELECT 1 FROM pools WHERE system IS NULL LIMIT 1") as cur:
        worker = await cur.fetchone() is not None

    dismissed = bool(
        (
            await service.get_setting(conn, path, service.GLOBAL, ONBOARDING_DISMISSED)
        ).value
    )
    orchestrator = bool(
        (
            await service.get_setting(conn, path, service.GLOBAL, ORCHESTRATOR_ENABLED)
        ).value
    )
    return OnboardingStatus(
        account=account,
        worker=worker,
        orchestrator=orchestrator,
        dismissed=dismissed,
    )


@router.get("/onboarding")
async def get_onboarding(
    db: DbDep, settings: SettingsDep, _: AdminDep
) -> OnboardingStatus:
    """Whether a fresh install has an account, a worker pool and the orchestrator."""
    async with db.read() as conn:
        return await status(conn, settings.model_policy_path)
