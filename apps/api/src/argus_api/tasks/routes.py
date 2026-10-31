import asyncio
from datetime import timedelta
from typing import Annotated, Any, Literal

from argus_core.auth import Scope
from argus_core.continuation import (
    ContinuationUpdate,
    EffectiveContinuation,
    ObservationRequest,
)
from argus_core.delivery import Delivery
from argus_core.task_edit import TaskAttempt, TaskEdit
from argus_core.tasks import (
    ENV_BLOCK_REASONS,
    ClaimedTask,
    Task,
    TaskCreate,
    TaskResult,
    TaskState,
    validate_milestone,
)
from fastapi import APIRouter, Query, Request, Response, status
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from argus_api.access import require_task_readable, worker_task_filter
from argus_api.accounts import service as accounts
from argus_api.accounts.routes import with_local_context
from argus_api.db import utcnow
from argus_api.deps import (
    AdminDep,
    AgentDep,
    CallerDep,
    DbDep,
    ManagerDep,
    SecretsDep,
    SettingsDep,
    TokenDep,
)
from argus_api.errors import ValidationFailedError
from argus_api.events import Event, OverflowedError
from argus_api.knowledge import service as knowledge
from argus_api.paging import DEFAULT_LIMIT, View
from argus_api.settings import service as settings_svc
from argus_api.tasks import (
    changes,
    delivery,
    log,
    prerequisites,
    service,
    steers,
    views,
)
from argus_api.tasks.prerequisites import PrerequisitesView
from argus_api.tasks.views import TaskPage

router = APIRouter(prefix="/tasks", tags=["tasks"])


class ClaimRequest(BaseModel):
    capabilities: list[str] = []
    wait_s: float = Field(default=0, ge=0, le=30)
    # Account providers the runtime can apply: a task restricted to others is skipped.
    runtime_providers: list[str] = []


class CompleteRequest(BaseModel):
    attempt: int
    result: TaskResult


class ProgressRequest(BaseModel):
    attempt: int
    message: str = Field(min_length=1, max_length=2000)
    percent: int | None = Field(default=None, ge=0, le=100)
    # A milestone (progress-milestones spec 3.1): a kind from the closed set and its data.
    kind: str | None = None
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _milestone(self) -> "ProgressRequest":
        validate_milestone(self.kind, self.data)
        return self


class LogRequest(BaseModel):
    attempt: int
    source: log.Source
    lines: list[str] = Field(min_length=1, max_length=log.MAX_LINES)


class FailRequest(BaseModel):
    attempt: int
    error: str = Field(min_length=1)
    reason: Literal[
        "worker_fail",
        "channel_error",
        "provider_failure",
        "quota",
        "account_unavailable",
        "runtime_switch_failed",
        "context_overflow",
        "verification_failed",
        "session_lost",
    ] = "worker_fail"
    """Why the attempt failed: the worker said so, or the channel detected it.
    `session_lost` is a vanished session container (stall visibility).
    `account_unavailable` is a lease or apply error: admission, like `quota`, so it
    spends no attempt. The
    difficulty raisers are `context_overflow` and `verification_failed`."""
    oom_kill: bool = False
    """The channel's evidence that the `session_lost` session was OOM-killed:
    Docker's `OOMKilled` with exit 137 on the container it saw (failure classes
    3.2.1). Only valid with `session_lost`."""


class DependenciesRequest(BaseModel):
    depends_on: list[str] = Field(max_length=service.MAX_DEPENDENCIES)
    expected_version: str = Field(min_length=1)  # the task's `version`


class BulkArchiveRequest(BaseModel):
    """Which terminal tasks to archive: all of `states`, narrowed by project and
    by how long ago they last changed. `dry_run` only counts."""

    states: list[TaskState] = Field(min_length=1)
    project: str | None = None
    older_than_days: int | None = Field(default=None, ge=0)
    dry_run: bool = False


class BulkArchiveResult(BaseModel):
    count: int  # tasks archived, or that would be with `dry_run`
    dry_run: bool


class ReleaseRequest(BaseModel):
    reason: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=service.RELEASE_REASON_MAX
        ),
    ]
    """Why the operator takes the task back (recorded on the event and attempt)."""
    attempt: int | None = None
    """Compare-and-set guard: release only if this is still the current attempt."""


class BlockRequest(BaseModel):
    attempt: int
    reason: str = Field(min_length=1)
    """An environment problem (failure classes 3.3): one of `ENV_BLOCK_REASONS`."""
    error: str | None = Field(default=None, max_length=1500)

    @field_validator("reason")
    @classmethod
    def _environment(cls, value: str) -> str:
        if value not in ENV_BLOCK_REASONS:
            raise ValueError(f"reason must be one of {sorted(ENV_BLOCK_REASONS)}")
        return value


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate, db: DbDep, caller: CallerDep, settings: SettingsDep
) -> Task:
    """Orchestrators and admins create any task; workers only subtasks of a task
    they hold. Past the per-agent hourly limit, an agent's new tasks wait for
    human approval (a `policy` decision) instead of being refused."""
    as_worker = caller.agent_id if caller.scope is Scope.WORKER else None
    limit = settings.max_tasks_per_agent_hour
    hourly_limit = limit if caller.agent_id is not None and limit > 0 else None
    async with db.write() as tx:
        return await service.create_task(
            tx, body, caller.actor, as_worker=as_worker, hourly_limit=hourly_limit
        )


@router.get("")
async def list_tasks(
    db: DbDep,
    caller: CallerDep,
    state: Annotated[list[TaskState] | None, Query()] = None,
    project: str | None = None,
    kind: str | None = None,
    assignee: str | None = None,
    archived: bool = False,
    ids: str | None = None,
    view: View = "summary",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    q: str | None = None,
) -> TaskPage:
    """Tasks newest first, in pages (max 100). `assignee` is the agent holding a
    task now (claimed tasks only). `ids` (comma-separated, max 50) is a batch read
    instead: no other filter, no paging, unknown ids in `missing`. Archived tasks
    are left out unless `archived=true`, which lists only those. Workers only see
    tasks they have leased. Optional `q` is literal substring search over title
    and spec (task discovery)."""
    scope = worker_task_filter(caller)
    async with db.snapshot() as snap:
        if ids is not None:
            if state or project or kind or assignee or archived or cursor or q:
                raise ValidationFailedError(
                    "ids is a batch read; it takes no other filter or cursor",
                    code="ids_exclusive",
                )
            return await views.get_tasks(snap, ids.split(","), view, task_scope=scope)
        return await views.list_tasks(
            snap,
            states=state,
            project=project,
            kind=kind,
            assignee=assignee,
            archived=archived,
            view=view,
            limit=limit,
            cursor=cursor,
            task_scope=scope,
            q=q,
        )


@router.post(
    "/claim",
    response_model=ClaimedTask,
    responses={204: {"description": "No matching task became ready within wait_s"}},
)
async def claim_task(
    request: Request,
    body: ClaimRequest,
    db: DbDep,
    agent_id: AgentDep,
    settings: SettingsDep,
    secrets: SecretsDep,
) -> ClaimedTask | Response:
    """Claim a ready task, long-polling up to `wait_s` seconds for one."""
    disk_guard = request.app.state.disk_guard
    loop = asyncio.get_running_loop()
    deadline = loop.time() + body.wait_s
    with db.bus.subscribe() as sub:
        while True:
            # Re-checked every pass (not only before the poll): a guard that
            # refuses while the caller waits gets `disk_low`, not a timeout.
            disk_guard.require_admission()
            async with db.write() as tx:
                task, engine_lease = await accounts.claim(
                    tx,
                    agent_id,
                    body.capabilities,
                    body.runtime_providers,
                    secrets,
                    default_timeout_s=settings.task_timeout_s,
                    disk_guard=disk_guard,
                )
            if task is not None:
                async with db.read() as conn:
                    refs = await knowledge.refs_for_task(
                        conn,
                        task,
                        now=utcnow(),
                        stale_after=timedelta(days=settings.knowledge_stale_days),
                    )
                    policy, revision = await settings_svc.effective_model_policy(
                        conn, settings.model_policy_path, task.project
                    )
                    switch = await service.attempt_model_switch(
                        conn, task.id, task.attempt
                    )
                    unknown = await steers.previous_unknown(conn, task.id, task.attempt)
                if engine_lease is not None:
                    engine_lease = await with_local_context(request, engine_lease)
                return ClaimedTask(
                    **task.model_dump(),
                    knowledge_refs=refs,
                    engine_lease=engine_lease,
                    model_policy=policy,
                    model_policy_revision=revision,
                    model_switch=switch,
                    steer_unknown=unknown,
                )
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    await sub.next_matching(_became_ready)
            except OverflowedError:
                continue  # missed events: just try to claim again
            except TimeoutError:
                return Response(status_code=status.HTTP_204_NO_CONTENT)


def _became_ready(event: Event) -> bool:
    return event.subject_type == "task" and event.data.get("to") == TaskState.READY


@router.get("/{task_id}")
async def get_task(task_id: str, db: DbDep, caller: CallerDep) -> views.TaskRead:
    async with db.snapshot() as snap:
        await require_task_readable(snap.conn, caller, task_id)
        return await views.read_task(snap.conn, task_id, snap.now)


@router.get("/{task_id}/delivery")
async def get_delivery(
    task_id: str, request: Request, db: DbDep, caller: CallerDep
) -> Delivery | None:
    """The state of the task's pull request as the forge shows it (open | merged |
    closed | unknown), when the task names one unambiguously, else null. Read-only:
    a merge flags the task for review and never closes or accepts it."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        task = await service.get_task(conn, task_id)
        return await delivery.delivery_of(conn, request.app.state.pr_checker, task)


@router.get("/{task_id}/prerequisites")
async def get_prerequisites(
    task_id: str,
    request: Request,
    db: DbDep,
    caller: CallerDep,
    expected_base: str | None = None,
) -> PrerequisitesView:
    """Evidence for each direct dependency: forge-merged on an expected base or a
    listed artifact re-verified, distinguished from task_succeeded. Read-only;
    enforced is always false (task discovery)."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        task = await service.get_task(conn, task_id)
        return await prerequisites.prerequisites_of(
            conn,
            request.app.state.pr_checker,
            request.app.state.artifacts,
            task,
            expected_base=expected_base,
        )


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: str,
    body: CompleteRequest,
    db: DbDep,
    agent_id: AgentDep,
    settings: SettingsDep,
) -> Task:
    async with db.write() as tx:
        return await service.complete(
            tx,
            agent_id,
            task_id,
            body.attempt,
            body.result,
            model_policy_path=settings.model_policy_path,
        )


@router.post("/{task_id}/progress")
async def report_progress(
    task_id: str, body: ProgressRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    """Post a progress note (at most one per 10 s). Also renews the lease."""
    async with db.write() as tx:
        return await service.report_progress(
            tx,
            agent_id,
            task_id,
            body.attempt,
            body.message,
            body.percent,
            body.kind,
            body.data,
        )


@router.post("/{task_id}/log")
async def post_log(
    task_id: str,
    body: LogRequest,
    db: DbDep,
    agent_id: AgentDep,
    token: TokenDep,
) -> log.LogAccepted:
    """Append output lines to the live log of the attempt you hold (agent token).
    Up to 200 lines of 2000 characters (longer ones are cut) and 64 KiB per batch,
    one batch per second per source. Secrets are redacted before storing; the
    batch is published as a `task.log` event and stored in a ring of 2000 lines
    per attempt. Fenced like progress: a stale attempt is 409 `stale_attempt`."""
    async with db.snapshot() as snap:  # refuse cheaply before the costly cleaning
        await log.admit(snap, agent_id, task_id, body.attempt, body.source)
    lines = log.prepare(
        body.lines, (token,) if token else ()
    )  # redaction stays outside the writer
    async with db.write() as tx:
        return await log.append(tx, agent_id, task_id, body.attempt, body.source, lines)


@router.put("/{task_id}/changes")
async def put_changes(
    task_id: str,
    body: changes.ChangesWrite,
    db: DbDep,
    agent_id: AgentDep,
    token: TokenDep,
) -> changes.ChangesAccepted:
    """Replace the diff snapshot of the attempt you hold (agent token), computed in
    the container against the workspace's merge base. At most 384 KiB, 500 files and
    a 256 KiB patch (longer is cut), one snapshot per 5 s. Secrets are redacted
    before storing; the summary is published as a `task.changes` event. Fenced like
    the live log: a stale attempt is 409 `stale_attempt`."""
    async with db.snapshot() as snap:  # refuse cheaply before the costly redaction
        await changes.admit(snap, agent_id, task_id, body.attempt)
    body = changes.prepare(body, (token,) if token else ())
    async with db.write() as tx:
        return await changes.store(tx, agent_id, task_id, body)


@router.get("/{task_id}/changes")
async def get_changes(
    task_id: str,
    db: DbDep,
    caller: CallerDep,
    attempt: Annotated[int | None, Query(ge=0, le=log.MAX_SEQ)] = None,
) -> changes.Changes:
    """The diff snapshot of an attempt (default: the current one); 404 `no_changes`
    when it has none. Workers only read tasks they have leased."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        return await changes.read(conn, task_id, attempt)


@router.post("/{task_id}/steers", status_code=status.HTTP_201_CREATED)
async def create_steer(
    task_id: str, body: steers.SteerCreate, db: DbDep, caller: ManagerDep
) -> steers.Steer:
    """Send an instruction to the attempt in flight. The assignee must report
    the `steer` feature. If the attempt ends first, the message expires, or ends
    `unknown` (it may have been delivered) when a heartbeat had carried it."""
    async with db.write() as tx:
        return await steers.create(tx, task_id, body.message, caller.actor)


@router.get("/{task_id}/steers")
async def list_steers(
    task_id: str,
    db: DbDep,
    _: ManagerDep,
    attempt: Annotated[int | None, Query(ge=0, le=log.MAX_SEQ)] = None,
) -> steers.SteerPage:
    """Steers for one attempt (the current one, when `attempt` is omitted), oldest first."""
    async with db.read() as conn:
        return await steers.list_steers(conn, task_id, attempt)


@router.delete("/{task_id}/steers/{steer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_steer(
    task_id: str, steer_id: str, db: DbDep, caller: ManagerDep
) -> None:
    """Cancel a pending steer. A delivered steer cannot be taken back."""
    async with db.write() as tx:
        await steers.cancel(tx, task_id, steer_id, caller.actor)


@router.get("/{task_id}/log")
async def get_log(
    task_id: str,
    db: DbDep,
    caller: CallerDep,
    attempt: Annotated[int | None, Query(ge=0, le=log.MAX_SEQ)] = None,
    after: Annotated[int | None, Query(ge=0, le=log.MAX_SEQ)] = None,
    limit: Annotated[int, Query(ge=1, le=log.MAX_READ)] = log.DEFAULT_TAIL,
) -> log.LogPage:
    """Log lines of an attempt (default: the current one), oldest first. With
    `after` (a `seq`), the lines after it; without, the newest `limit` lines.
    Workers only read tasks they have leased."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        return await log.read(conn, task_id, attempt, after, limit)


@router.post("/{task_id}/fail")
async def fail_task(
    task_id: str,
    body: FailRequest,
    db: DbDep,
    agent_id: AgentDep,
    secrets: SecretsDep,
    settings: SettingsDep,
) -> Task:
    """Fail the attempt. A `provider_failure` that is a known quota notice of the
    agent's engine account is not a failure: the account is exhausted, the task
    returns to ready (or waits as `awaiting_quota`) and `failures` is unchanged."""
    async with db.write() as tx:
        return await accounts.fail_task(
            tx,
            agent_id,
            task_id,
            body.attempt,
            body.error,
            body.reason,
            secrets,
            timedelta(minutes=settings.account_cooldown_minutes),
            oom_kill=body.oom_kill,
        )


@router.post("/{task_id}/block")
async def block_task(
    task_id: str, body: BlockRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    async with db.write() as tx:
        return await service.block(
            tx, agent_id, task_id, body.attempt, body.reason, body.error
        )


@router.post("/{task_id}/unblock")
async def unblock_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    async with db.write() as tx:
        return await service.unblock(tx, task_id, caller.actor)


@router.post("/{task_id}/retry")
async def retry_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    """Return a `failed` task to `ready` with a fresh window of attempts (409 otherwise)."""
    async with db.write() as tx:
        return await service.retry(tx, task_id, caller.actor)


@router.post("/{task_id}/release")
async def release_task(
    task_id: str, body: ReleaseRequest, db: DbDep, caller: ManagerDep
) -> Task:
    """Take a claimed task back from its holder and requeue it without counting a
    failed attempt (orchestrator or admin). 409 `task_not_claimed` in any other
    state, 409 `stale_attempt` when `attempt` is not the current one. The holder's
    next write for that attempt gets 409 and its heartbeat rejects the pair."""
    async with db.write() as tx:
        return await service.release(
            tx, task_id, caller.actor, body.reason, attempt=body.attempt
        )


@router.patch("/{task_id}")
async def edit_task(
    task_id: str, body: TaskEdit, db: DbDep, caller: ManagerDep
) -> Task:
    """Edit a task's title, spec, acceptance, priority, required capabilities,
    knowledge pins or dependencies (orchestrator or admin). `changes` is a partial
    patch: omitted fields stay, `null` clears. Which fields a state accepts is
    `editable_fields` on the task. Stale `expected_version` is 409 `stale_version`,
    a field the state does not allow 409 `task_not_editable`, an immutable or unknown
    field 422 `immutable_field`; a refused edit writes nothing."""
    async with db.write() as tx:
        return await service.edit_task(
            tx, task_id, body.changes, body.expected_version, caller.actor
        )


@router.get("/{task_id}/continuation")
async def get_continuation(
    task_id: str, db: DbDep, caller: CallerDep
) -> EffectiveContinuation:
    """The continuation contract: phase, known artifacts with provenance, preserved
    WIP, last interruption, checklist with evidence, effective decisions and what
    Argus does not know (`gaps`). Derived from stored facts; unknown is not absent."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        return await service.continuation_of(conn, task_id)


@router.patch("/{task_id}/continuation")
async def update_continuation(
    task_id: str, body: ContinuationUpdate, db: DbDep, caller: ManagerDep
) -> Task:
    """Settle continuation facts (orchestrator or admin): phase, artifacts, WIP,
    checklist items and evidence, environment status, retire decisions. Written with
    orchestrator provenance; stale `expected_revision` is 409 `stale_continuation`."""
    async with db.write() as tx:
        return await service.update_continuation(tx, task_id, body, caller.actor)


@router.post("/{task_id}/observations")
async def observe_task(
    task_id: str, body: ObservationRequest, db: DbDep, agent_id: AgentDep
) -> Task:
    """The holder's runtime reports live observations (artifact state, environment
    readiness, preserved WIP) made outside any transaction. Idempotent; fenced by
    the attempt like every worker write."""
    async with db.write() as tx:
        return await service.observe(tx, agent_id, task_id, body)


@router.get("/{task_id}/attempts")
async def list_attempts(
    task_id: str, db: DbDep, caller: CallerDep
) -> list[TaskAttempt]:
    """The definition each attempt was handed at claim, oldest first."""
    async with db.read() as conn:
        await require_task_readable(conn, caller, task_id)
        return await service.attempt_definitions(conn, task_id)


@router.put("/{task_id}/dependencies")
async def set_dependencies(
    task_id: str, body: DependenciesRequest, db: DbDep, caller: ManagerDep
) -> Task:
    """Replace the dependencies of a task that is still waiting (pending,
    awaiting_approval or ready). A ready task with an unfinished new dependency
    goes back to pending; a pending one whose dependencies all succeeded becomes
    ready. Refuses cycles (422 dependency_cycle) and stale versions (409)."""
    async with db.write() as tx:
        return await service.set_dependencies(
            tx, task_id, body.depends_on, body.expected_version, caller.actor
        )


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    async with db.write() as tx:
        return await service.cancel(tx, task_id, caller.actor)


@router.post("/archive")
async def archive_tasks(
    body: BulkArchiveRequest, db: DbDep, caller: ManagerDep
) -> BulkArchiveResult:
    """Archive terminal tasks by state, project and age, answering how many.
    `dry_run` counts without writing, for a confirmation. Non-terminal states
    are refused (422 `state_not_archivable`) before anything changes."""
    async with db.write() as tx:
        count = await service.archive_matching(
            tx,
            caller.actor,
            states=body.states,
            project=body.project,
            older_than_days=body.older_than_days,
            dry_run=body.dry_run,
        )
    return BulkArchiveResult(count=count, dry_run=body.dry_run)


@router.post("/{task_id}/archive")
async def archive_task(task_id: str, db: DbDep, caller: ManagerDep) -> Task:
    """Archive a `succeeded`, `failed` or `cancelled` task; repeating it changes
    nothing. Any other state answers 409 `task_not_terminal` and changes nothing."""
    async with db.write() as tx:
        return await service.archive(tx, task_id, caller.actor)


@router.post("/{task_id}/unarchive")
async def unarchive_task(task_id: str, db: DbDep, caller: AdminDep) -> Task:
    """Return an archived task to listings and totals (admin). Idempotent."""
    async with db.write() as tx:
        return await service.unarchive(tx, task_id, caller.actor)
