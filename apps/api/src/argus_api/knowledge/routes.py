from contextlib import suppress
from datetime import timedelta
from typing import Annotated

from argus_core.auth import Scope
from argus_core.knowledge import (
    SLUG_PATTERN,
    KnowledgeWrite,
    Page,
    PageCreate,
    PageEdit,
    PageListItem,
    PageRestore,
    Project,
    ProjectCheckout,
    ProjectCheckoutRequest,
    ProjectRepository,
    ProjectRepositoryChange,
    ProjectStats,
    ProjectUpsert,
    Proposal,
    ProposalState,
    Revision,
    SearchHit,
)
from argus_core.tasks import TaskCreate
from fastapi import APIRouter, Path, Query, status

from argus_api.access import (
    is_global_reader,
    leased_projects,
    require_page_readable,
    require_project_readable,
    worker_project_filter,
)
from argus_api.auth import Identity
from argus_api.config import Settings
from argus_api.db import Database, utcnow
from argus_api.deps import AdminDep, CallerDep, DbDep, ManagerDep, SettingsDep
from argus_api.errors import ConflictError, ForbiddenError, NotFoundError
from argus_api.knowledge import service

router = APIRouter(tags=["knowledge"])

Slug = Annotated[str, Path(pattern=SLUG_PATTERN)]


def _stale(settings: Settings) -> timedelta:
    return timedelta(days=settings.knowledge_stale_days)


def _proposes(caller: Identity, settings: Settings) -> bool:
    return (
        caller.scope is Scope.WORKER and settings.knowledge_worker_writes == "propose"
    )


async def _page(db: Database, page_id: str, settings: Settings) -> Page:
    async with db.read() as conn:
        return await service.get_page(
            conn, page_id, now=utcnow(), stale_after=_stale(settings)
        )


# -- projects -------------------------------------------------------------------------


@router.get("/projects")
async def list_projects(db: DbDep, caller: CallerDep) -> list[Project]:
    async with db.read() as conn:
        projects = await service.list_projects(conn)
        if is_global_reader(caller):
            return projects
        assert caller.agent_id is not None
        allowed = await leased_projects(conn, caller.agent_id)
        return [p for p in projects if p.slug in allowed]


@router.get("/projects/stats")
async def project_stats(db: DbDep, caller: ManagerDep) -> list[ProjectStats]:
    """Per-project counts and the repository workers would resolve (not for workers:
    it spans every project)."""
    async with db.read() as conn:
        return await service.project_stats(conn)


@router.put("/projects/{slug}")
async def upsert_project(
    slug: Slug, body: ProjectUpsert, db: DbDep, caller: ManagerDep
) -> Project:
    async with db.write() as tx:
        if "repository_url" in body.model_fields_set and caller.scope != Scope.ADMIN:
            # The URL decides which host a repository credential is sent to, so
            # only an admin may change it (an unchanged value is fine).
            current = None
            with suppress(NotFoundError):
                current = (await service.get_project(tx.conn, slug)).repository_url
            if body.repository_url != current:
                raise ForbiddenError(
                    "only an admin can change a project's repository",
                    code="repository_admin_only",
                )
        return await service.upsert_project(
            tx, slug, body, caller.actor, admin=caller.scope == Scope.ADMIN
        )


@router.get("/projects/{slug}/repository")
async def get_project_repository(
    slug: Slug, db: DbDep, caller: CallerDep
) -> ProjectRepository:
    """The repository Argus resolves for the project (its record, else the one every
    pool names), or why there is none: `unset`, `ambiguous` or `invalid`. Workers
    only read projects they have leased."""
    async with db.read() as conn:
        await require_project_readable(conn, caller, slug)
        return await service.project_repository(conn, slug)


@router.post("/projects/{slug}/checkout", status_code=status.HTTP_201_CREATED)
async def checkout_project(
    slug: Slug, body: ProjectCheckoutRequest, db: DbDep, caller: ManagerDep
) -> ProjectCheckout:
    """Start a checkout of the project's configured repository: a read-only task
    with no capability and no deliverable beyond a summary. The worker channel
    prepares its isolated workspace through the task-scoped repository lease, so
    this grants no write access and no credential reaches the caller."""
    # Lazy: the task service imports the knowledge service.
    from argus_api.tasks import service as tasks

    async with db.write() as tx:
        repo = await service.project_repository(tx.conn, slug)
        if repo.repository_url is None:
            raise ConflictError(repo.detail, code=f"repository_{repo.status}")
        where = f" at {body.ref}" if body.ref else ""
        task = await tasks.create_task(
            tx,
            TaskCreate(
                title=f"Check out {repo.repository_url.removeprefix('https://')}"[:200],
                project=slug,
                spec=(
                    f"Read-only checkout of {repo.repository_url}{where}. Fetch "
                    f"{body.ref or 'the default branch'} into the prepared "
                    "workspace, confirm HEAD and the remote, and report the commit "
                    "SHA. Do not push or open a PR."
                ),
            ),
            caller.actor,
            as_worker=None,
            hourly_limit=None,
            idempotency_key=None,
        )
    return ProjectCheckout(
        project=slug,
        repository_url=repo.repository_url,
        ref=body.ref,
        task_id=task.id,
        workspace=f"task-workspaces/{task.id}",
        branch=f"argus/task-{task.id[:8]}",
    )


@router.patch("/projects/{slug}/repository")
async def set_project_repository(
    slug: Slug, body: ProjectRepositoryChange, db: DbDep, caller: AdminDep
) -> Project:
    """Admin only: set (or with null clear) just the repository. Unlike the full PUT it
    never touches name or description, so it cannot overwrite a concurrent edit."""
    async with db.write() as tx:
        return await service.set_repository(tx, slug, body.repository_url, caller.actor)


# -- pages -------------------------------------------------------------------------------


@router.get("/knowledge/pages")
async def list_pages(
    db: DbDep,
    caller: CallerDep,
    settings: SettingsDep,
    project: str | None = None,
    include_archived: bool = False,
) -> list[PageListItem]:
    async with db.read() as conn:
        return await service.list_pages(
            conn,
            now=utcnow(),
            project=project,
            include_archived=include_archived,
            stale_after=_stale(settings),
            project_scope=worker_project_filter(caller),
        )


@router.get("/knowledge/search")
async def search(
    db: DbDep,
    caller: CallerDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    project: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[SearchHit]:
    """Full-text search over active pages (all words must match; if nothing does,
    any word may). Stale pages rank after fresh ones."""
    scope = worker_project_filter(caller, column="p.project")
    async with db.read() as conn:
        for match_any in (False, True):
            hits = await service.search(
                conn,
                q,
                now=utcnow(),
                project=project,
                limit=limit,
                match_any=match_any,
                stale_after=_stale(settings),
                project_scope=scope,
            )
            if hits:
                break
        return hits


@router.post("/knowledge/pages", status_code=status.HTTP_201_CREATED)
async def create_page(
    body: PageCreate, db: DbDep, caller: CallerDep, settings: SettingsDep
) -> KnowledgeWrite:
    """Create a page. Workers may be limited to proposing (see
    ARGUS_KNOWLEDGE_WORKER_WRITES); the response says which happened."""
    async with db.write() as tx:
        if _proposes(caller, settings):
            proposal_id = await service.propose(
                tx,
                caller.actor,
                content=body,
                project=body.project,
                slug=body.slug,
                page_id=None,
                base_revision_id=None,
            )
            proposal = await service.get_proposal(tx.conn, proposal_id)
            return KnowledgeWrite(status="proposed", proposal=proposal)
        page_id = await service.create_page(tx, body, caller.actor)
    return KnowledgeWrite(status="published", page=await _page(db, page_id, settings))


@router.get("/knowledge/pages/{page_id}")
async def get_page(
    page_id: str,
    db: DbDep,
    caller: CallerDep,
    settings: SettingsDep,
    revision: int | None = None,
) -> Page:
    async with db.read() as conn:
        page = await service.get_page(
            conn, page_id, now=utcnow(), stale_after=_stale(settings), revision=revision
        )
        await require_page_readable(conn, caller, page.id, page.project)
        return page


@router.get("/knowledge/pages/{page_id}/revisions")
async def list_revisions(
    page_id: str, db: DbDep, caller: CallerDep, settings: SettingsDep
) -> list[Revision]:
    async with db.read() as conn:
        page = await service.get_page(
            conn, page_id, now=utcnow(), stale_after=_stale(settings)
        )
        await require_page_readable(conn, caller, page.id, page.project)
        return await service.list_revisions(conn, page_id)


@router.post(
    "/knowledge/pages/{page_id}/revisions", status_code=status.HTTP_201_CREATED
)
async def edit_page(
    page_id: str, body: PageEdit, db: DbDep, caller: CallerDep, settings: SettingsDep
) -> KnowledgeWrite:
    """Publish an edit. `base_revision_id` must be the page's current revision,
    otherwise `409 knowledge_conflict`: re-read the page and apply the change again."""
    async with db.write() as tx:
        if _proposes(caller, settings):
            page = await service.get_page(tx.conn, page_id, now=tx.now)
            proposal_id = await service.propose(
                tx,
                caller.actor,
                content=body,
                project=page.project,
                slug=page.slug,
                page_id=page_id,
                base_revision_id=body.base_revision_id,
            )
            proposal = await service.get_proposal(tx.conn, proposal_id)
            return KnowledgeWrite(status="proposed", proposal=proposal)
        await service.edit_page(tx, page_id, body, caller.actor)
    return KnowledgeWrite(status="published", page=await _page(db, page_id, settings))


@router.post("/knowledge/pages/{page_id}/restore")
async def restore_page(
    page_id: str,
    body: PageRestore,
    db: DbDep,
    caller: ManagerDep,
    settings: SettingsDep,
) -> Page:
    async with db.write() as tx:
        await service.restore(tx, page_id, body, caller.actor)
    return await _page(db, page_id, settings)


@router.post("/knowledge/pages/{page_id}/archive")
async def archive_page(
    page_id: str, db: DbDep, caller: ManagerDep, settings: SettingsDep
) -> Page:
    async with db.write() as tx:
        await service.set_archived(tx, page_id, True, caller.actor)
    return await _page(db, page_id, settings)


@router.post("/knowledge/pages/{page_id}/unarchive")
async def unarchive_page(
    page_id: str, db: DbDep, caller: ManagerDep, settings: SettingsDep
) -> Page:
    async with db.write() as tx:
        await service.set_archived(tx, page_id, False, caller.actor)
    return await _page(db, page_id, settings)


@router.post("/knowledge/pages/{page_id}/reviewed")
async def mark_reviewed(
    page_id: str, db: DbDep, caller: ManagerDep, settings: SettingsDep
) -> Page:
    """Confirm the page is still right; resets its freshness."""
    async with db.write() as tx:
        await service.mark_reviewed(tx, page_id, caller.actor)
    return await _page(db, page_id, settings)


# -- proposals ---------------------------------------------------------------------------


@router.get("/knowledge/proposals")
async def list_proposals(
    db: DbDep, caller: CallerDep, state: ProposalState | None = ProposalState.OPEN
) -> list[Proposal]:
    """Proposals are resolved by answering their `knowledge` decision."""
    async with db.read() as conn:
        return await service.list_proposals(
            conn, state, project_scope=worker_project_filter(caller)
        )


@router.get("/knowledge/proposals/{proposal_id}")
async def get_proposal(proposal_id: str, db: DbDep, caller: CallerDep) -> Proposal:
    async with db.read() as conn:
        proposal = await service.get_proposal(conn, proposal_id)
        await require_project_readable(conn, caller, proposal.project)
        return proposal
