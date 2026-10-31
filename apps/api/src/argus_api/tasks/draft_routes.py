"""REST for task drafts (docs/specs/task-drafts.spec.md 3.6): orchestrator or admin."""

from argus_core.task_drafts import (
    PublishRequest,
    PublishResult,
    TaskDraft,
    TaskDraftBody,
    TaskDraftEdit,
    TaskDraftPage,
)
from fastapi import APIRouter, Query, status

from argus_api.deps import DbDep, ManagerDep, SettingsDep
from argus_api.tasks import drafts

router = APIRouter(prefix="/task-drafts", tags=["task-drafts"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_draft(body: TaskDraftBody, db: DbDep, caller: ManagerDep) -> TaskDraft:
    """Save a task definition that nothing can claim until it is published. Field
    rules are the task's; title, spec, acceptance and project may wait for publish."""
    async with db.write() as tx:
        return await drafts.create_draft(tx, body, caller.actor)


@router.get("")
async def list_drafts(db: DbDep, _: ManagerDep) -> TaskDraftPage:
    """Open drafts, oldest first; at most 200 exist, so the list is whole."""
    async with db.read() as conn:
        return await drafts.list_drafts(conn)


@router.post("/publish")
async def publish_drafts(
    body: PublishRequest, db: DbDep, caller: ManagerDep, settings: SettingsDep
) -> PublishResult:
    """Validate drafts and create their tasks in one transaction. `atomic` (the
    default) publishes all or none; `partial` publishes each valid draft and reports
    the others. In-batch `depends_on_drafts` become the new task ids. A repeat with
    the same `idempotency_key` and request returns the stored answer."""
    limit = settings.max_tasks_per_agent_hour
    hourly_limit = limit if caller.agent_id is not None and limit > 0 else None
    async with db.write() as tx:
        return await drafts.publish(tx, body, caller.actor, hourly_limit=hourly_limit)


@router.get("/{draft_id}")
async def get_draft(draft_id: str, db: DbDep, _: ManagerDep) -> TaskDraft:
    async with db.read() as conn:
        return await drafts.get_draft(conn, draft_id)


@router.patch("/{draft_id}")
async def edit_draft(
    draft_id: str, body: TaskDraftEdit, db: DbDep, caller: ManagerDep
) -> TaskDraft:
    """Patch an open draft: omitted fields stay, `null` resets one. A stale
    `expected_version` is 409 `stale_version`; a published draft 409
    `draft_published`."""
    async with db.write() as tx:
        return await drafts.edit_draft(
            tx, draft_id, body.changes, body.expected_version, caller.actor
        )


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_draft(
    draft_id: str,
    db: DbDep,
    _: ManagerDep,
    expected_version: str = Query(min_length=1),
) -> None:
    async with db.write() as tx:
        await drafts.discard_draft(tx, draft_id, expected_version)
