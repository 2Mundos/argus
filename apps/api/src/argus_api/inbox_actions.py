"""The inbox's actionable projection (MCP operations 3.2, "Actionable projection").

The same rows as `inbox.inbox`, each with what a manager needs to act on it: the
question, who may answer, what each answer does, what it holds up, and the
evidence Argus has, labelled by source and freshness. Read-only and advisory:
nothing here answers, approves or changes anything, and the server re-checks
every real call. Live forge reads are opt-in, bounded, and run after the DB
snapshot closes; they are never part of it.
"""

import asyncio
from collections import deque
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from argus_core.auth import Scope
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ContinuationProvenance,
    ItemStatus,
    split_spec,
)
from argus_core.decisions import Audience, Decision, DecisionKind
from argus_core.delivery import MERGED_PENDING_REVIEW, PrState
from argus_core.redact import redact
from argus_core.tasks import Review, Task, TaskState
from pydantic import BaseModel

from argus_api.db import Database, Snapshot
from argus_api.decisions import store as decisions
from argus_api.inbox import (
    BlockedTask,
    InboxItem,
    InboxPage,
    Order,
    _dependents,
    inbox,
)
from argus_api.tasks import delivery
from argus_api.tasks import service as tasks_service
from argus_api.tasks.delivery import PrChecker, PrRef

MAX_LIMIT = 25
PAGE_BYTES = 64_000  # the page's JSON; at least one row is always returned
QUESTION_CAP = 2048
EXCERPT_CAP = 512
ITEM_CAP = 200  # one checklist item or WIP summary
LIST_CAP = 10
STALE_S = 3600  # a stored observation older than this, at snapshot time, is stale
FORGE_READS = 5
FORGE_BUDGET_S = 6.0

Source = Literal["worker_claim", "stored_observation", "live_forge"]
Status = Literal["unknown", "stale", "conflict", "fresh"]
ReadError = Literal[
    "not_requested", "no_reference", "read_budget", "timeout", "unavailable"
]
Action = Literal[
    "answer_decision",
    "escalate_to_human",
    "unblock_task",
    "retry_dependency",
    "inspect_stall",
    "reread",
    "none",
]

AUTHORITY_NOTE = (
    "Hints only: Argus executes nothing from this view. answer_decision, task"
    " actions and reviews re-check scope, audience, state and options on the call."
)
_ANSWERABLE = {
    Audience.HUMAN: [Scope.ADMIN],
    Audience.ORCHESTRATOR: [Scope.ORCHESTRATOR, Scope.ADMIN],
    Audience.ANY: [Scope.ORCHESTRATOR, Scope.ADMIN],
}
_KNOWN_PR = {ArtifactState.OPEN, ArtifactState.MERGED, ArtifactState.CLOSED}


class Text(BaseModel):
    text: str  # redacted; the whole value unless `truncated`
    bytes: int  # the whole (redacted) value's length
    truncated: bool
    ref: str | None  # the read that returns the whole item, when truncated


class Evidence(BaseModel):
    value: str | None  # None: nothing known
    source: Source | None
    observed_at: datetime | None
    status: Status
    error: ReadError | None = None  # why a live read gave nothing
    truncated: bool = False


class Authority(BaseModel):
    audience: Audience | None  # None: a task action, not a decision
    answerable_by: list[Scope]
    caller_role: Scope
    caller_may_attempt: bool  # not refused up front; never a grant
    reason: str


class Effect(BaseModel):
    choice: str  # an option, or "text" for a free-text answer
    effect: str


class NextAction(BaseModel):
    action: Action  # a hint; never executed by Argus
    args: dict[str, str]
    reason: str


class ImpactList(BaseModel):
    count: int
    items: list[BlockedTask]
    omitted: int


class Impact(BaseModel):
    direct: ImpactList  # non-terminal tasks that depend on the row's task
    transitive: ImpactList  # reachable beyond them; each task counted once


class PendingItem(BaseModel):
    id: str
    text: str
    status: ItemStatus
    source: Source


class Checklist(BaseModel):
    state: Literal["structured", "unstructured", "unknown"]
    total: int
    pending_count: int
    pending: list[PendingItem]
    omitted: int


class DeliveryEvidence(BaseModel):
    status: Status
    pr: str | None  # canonical URL: no credentials, query or fragment
    headline: Evidence  # the newer of stored and live
    stored: Evidence  # the newest PR artifact in the continuation
    live: Evidence  # read now with forge=true
    flags: list[str]


class Actionable(BaseModel):
    question: Text | None
    spec_excerpt: Text | None
    options: list[str] | None
    authority: Authority
    effects: list[Effect] | Literal["unknown"]
    next: NextAction
    impact: Impact
    checklist: Checklist
    wip: Evidence
    transfer: Evidence
    delivery: DeliveryEvidence
    ci: Evidence  # not stored by Argus: always unknown (an extension point)
    decision_state_changed: bool = False  # answered or cancelled during this read


class ActionableInboxItem(InboxItem):
    actionable: Actionable


class ExternalReads(BaseModel):
    in_snapshot: Literal[False] = False  # live values are newer than snapshot_at
    requested: bool
    started_at: datetime | None
    finished_at: datetime | None
    budget_s: float
    max_reads: int
    reads: int
    timed_out: int


class ActionableInboxPage(InboxPage):
    # Narrowed on purpose: every row of this page is projected.
    items: list[ActionableInboxItem]  # ty: ignore[invalid-attribute-override]
    snapshot_at: datetime  # the one read transaction behind every stored value
    external: ExternalReads
    authority_note: str = AUTHORITY_NOTE


_UNKNOWN = Evidence(value=None, source=None, observed_at=None, status="unknown")


async def actionable_inbox(
    db: Database,
    checker: PrChecker,
    caller: Scope,
    *,
    forge: bool,
    project: str | None,
    states: list[TaskState] | None,
    decision_kinds: list[DecisionKind] | None,
    blocking_only: bool,
    order: Order,
    limit: int,
    cursor: str | None,
) -> ActionableInboxPage:
    projection = {"view": "actionable", "forge": forge}
    async with db.snapshot() as snap:
        page = await inbox(
            snap,
            project=project,
            states=states,
            decision_kinds=decision_kinds,
            blocking_only=blocking_only,
            order=order,
            limit=min(limit, MAX_LIMIT),
            cursor=cursor,
            projection=projection,
        )
        snapshot_at = snap.now
        found = await _decisions(snap, [i.decision_id for i in page.items])
        tasks = {
            i.subject_id: await tasks_service.get_task(snap.conn, i.subject_id)
            for i in page.items
            if i.subject_type == "task"
        }
        graph = await _dependents(snap)
        refs = {}
        if forge:
            for task in tasks.values():
                if (ref := await delivery.reference(snap.conn, task)) is not None:
                    refs[task.id] = ref
    # The snapshot is closed: nothing below is part of it.
    external = ExternalReads(
        requested=forge,
        started_at=None,
        finished_at=None,
        budget_s=FORGE_BUDGET_S,
        max_reads=FORGE_READS,
        reads=0,
        timed_out=0,
    )
    live: dict[str, Evidence] = {}
    changed: set[str] = set()
    if forge:
        external.started_at = db.now()
        live, external.reads, external.timed_out = await _read_forge(db, checker, refs)
        external.finished_at = db.now()
        changed = await _no_longer_open(db, list(found))
    items = []
    for item in page.items:
        task = tasks.get(item.subject_id) if item.subject_type == "task" else None
        decision = found.get(item.decision_id or "")
        if not forge:
            read = _missing("not_requested")
        elif task is None:
            read = _UNKNOWN
        elif task.id in live:
            read = live[task.id]
        else:
            read = _missing("read_budget" if task.id in refs else "no_reference")
        actionable = _project(
            item,
            task,
            decision,
            graph,
            caller,
            snapshot_at,
            ref=refs.get(task.id) if task else None,
            live=read,
            changed=item.decision_id in changed,
        )
        items.append(
            ActionableInboxItem.model_validate(
                {**item.model_dump(), "actionable": actionable}
            )
        )
    return _fit(page, items, snapshot_at, external)


def _fit(
    page: InboxPage,
    items: list[ActionableInboxItem],
    snapshot_at: datetime,
    external: ExternalReads,
) -> ActionableInboxPage:
    """Keep rows while the page's JSON stays under PAGE_BYTES (at least one row); a
    cut page resumes after its last row."""
    out = ActionableInboxPage.model_validate(
        {
            **page.model_dump(exclude={"items"}),
            "items": [],
            "snapshot_at": snapshot_at,
            "external": external,
        }
    )
    size = len(out.model_dump_json())
    kept = 0
    for item in items:
        size += len(item.model_dump_json()) + 1
        if kept and size > PAGE_BYTES:
            break
        kept += 1
    out.items = items[:kept]
    if kept < len(items):
        out.has_more = True
        out.next_cursor = page.items[kept - 1]._cursor
        out.omitted = sorted({*out.omitted, "budget_cut"})
    return out


def _project(
    item: InboxItem,
    task: Task | None,
    decision: Decision | None,
    graph: dict[str, list[BlockedTask]],
    caller: Scope,
    now: datetime,
    *,
    ref: PrRef | None,
    live: Evidence,
    changed: bool,
) -> Actionable:
    authority = _authority(decision, task, caller)
    return Actionable(
        question=_text(
            decision.question, QUESTION_CAP, f"GET /api/decisions/{decision.id}"
        )
        if decision
        else None,
        spec_excerpt=_text(
            split_spec(task.spec)[0], EXCERPT_CAP, f"GET /api/tasks/{task.id}"
        )
        if task
        else None,
        options=decision.options if decision else None,
        authority=authority,
        effects=_effects(decision) if decision else [],
        next=_next(item, task, decision, authority, changed),
        impact=_impact(item.subject_id, graph if task else {}),
        checklist=_checklist(task),
        wip=_wip(task, now),
        transfer=_transfer(task, now),
        delivery=_delivery(task, now, ref, live),
        ci=_UNKNOWN,
        decision_state_changed=changed,
    )


def _text(value: str, cap: int, ref: str) -> Text:
    value = redact(value)
    data = value.encode()
    if len(data) <= cap:
        return Text(text=value, bytes=len(data), truncated=False, ref=None)
    cut = data[:cap].decode(errors="ignore")
    return Text(text=cut, bytes=len(data), truncated=True, ref=ref)


def _short(value: str) -> tuple[str, bool]:
    text = _text(value, ITEM_CAP, "")
    return text.text, text.truncated


def _authority(
    decision: Decision | None, task: Task | None, caller: Scope
) -> Authority:
    if decision is None:
        return Authority(
            audience=None,
            answerable_by=[Scope.ORCHESTRATOR, Scope.ADMIN],
            caller_role=caller,
            caller_may_attempt=True,
            reason="a task action: any manager may call it",
        )
    allowed = _ANSWERABLE[decision.audience]
    reason = f"decision audience is {decision.audience}"
    if (
        decision.kind is DecisionKind.REVIEW
        and task is not None
        and task.review is Review.HUMAN
    ):
        allowed, reason = [Scope.ADMIN], "the task requires human review"
    return Authority(
        audience=decision.audience,
        answerable_by=allowed,
        caller_role=caller,
        caller_may_attempt=caller in allowed,
        reason=reason,
    )


def _effects(decision: Decision) -> list[Effect] | Literal["unknown"]:
    """What each answer does, from decisions.service._apply_answer."""
    kind, subject = decision.kind, decision.subject_type

    def each(yes: str, effect_yes: str, effect_no: str) -> list[Effect]:
        return [
            Effect(choice=o, effect=effect_yes if o == yes else effect_no)
            for o in decision.options or []
        ]

    if kind is DecisionKind.QUESTION and subject == "task":
        effect = (
            "the answer becomes a clarification on the task; the task returns to ready"
        )
        return [Effect(choice=o, effect=effect) for o in decision.options or ["text"]]
    if kind is DecisionKind.REVIEW and subject == "task":
        return each(
            "accept",
            "the task succeeds",
            "the task returns to ready with the notes appended to its spec; no"
            " failure is counted",
        )
    if kind is DecisionKind.POLICY and subject == "pool":
        return each("approve", "applies the requested scale", "keeps the current scale")
    if kind is DecisionKind.POLICY and subject == "routine":
        return each("approve", "re-enables the routine", "the routine stays disabled")
    if kind in (DecisionKind.APPROVAL, DecisionKind.POLICY) and subject == "task":
        return each(
            "approve", "approves the task: it can be claimed", "cancels the task"
        )
    if kind is DecisionKind.KNOWLEDGE and subject == "knowledge_proposal":
        return each("accept", "applies the proposal", "rejects the proposal")
    return "unknown"


def _next(
    item: InboxItem,
    task: Task | None,
    decision: Decision | None,
    authority: Authority,
    changed: bool,
) -> NextAction:
    if changed:
        return NextAction(
            action="reread",
            args={"decision_id": item.decision_id or ""},
            reason="the decision was answered or cancelled during this read",
        )
    if decision is not None:
        args = {"decision_id": decision.id}
        if not authority.caller_may_attempt:
            return NextAction(
                action="escalate_to_human",
                args=args,
                reason=f"{authority.reason}; this caller may not answer it",
            )
        reason = f"open {decision.kind} decision; choose from its effects"
        if (
            decision.kind is DecisionKind.REVIEW
            and task
            and task.continuation
            and task.continuation.pending
        ):
            reason += "; checklist items are still pending"
        return NextAction(action="answer_decision", args=args, reason=reason)
    if task is None:
        return NextAction(action="none", args={}, reason="nothing to act on")
    args = {"task_id": task.id}
    if task.state is TaskState.BLOCKED:
        why, _ = _short(task.block_reason or "unknown")
        return NextAction(
            action="unblock_task",
            args=args,
            reason=f"blocked without an open decision ({why}); unblock it once the"
            " cause is fixed",
        )
    if item.failed_dependencies:
        return NextAction(
            action="retry_dependency",
            args={"task_id": item.failed_dependencies[0]},
            reason="waits on a failed or cancelled dependency: retry it or"
            " set_task_dependencies",
        )
    if task.state is TaskState.CLAIMED and item.stall_cause is not None:
        return NextAction(
            action="inspect_stall",
            args=args,
            reason=f"quiet claim ({item.stall_cause}): get_task_log, then"
            " steer_task or release_task",
        )
    return NextAction(action="none", args={}, reason="nothing waits on a manager")


def _impact(task_id: str, graph: dict[str, list[BlockedTask]]) -> Impact:
    direct = [d for d in graph.get(task_id, []) if d.id != task_id]
    seen = {task_id, *(d.id for d in direct)}
    beyond: list[BlockedTask] = []
    queue = deque(d.id for d in direct)
    while queue:
        for d in graph.get(queue.popleft(), []):
            if d.id not in seen:
                seen.add(d.id)
                beyond.append(d)
                queue.append(d.id)
    return Impact(direct=_list(direct), transitive=_list(beyond))


def _list(tasks: list[BlockedTask]) -> ImpactList:
    shown = tasks[:LIST_CAP]
    return ImpactList(count=len(tasks), items=shown, omitted=len(tasks) - len(shown))


def _source(provenance: ContinuationProvenance) -> Source:
    """Anything the worker holding the task sent (a report, or what its runtime
    says it observed) is a claim: Argus has not checked it."""
    if provenance in (ContinuationProvenance.WORKER, ContinuationProvenance.RUNTIME):
        return "worker_claim"
    return "stored_observation"


def _stored(
    value: str | None,
    provenance: ContinuationProvenance,
    observed_at: datetime | None,
    now: datetime,
    truncated: bool = False,
) -> Evidence:
    if value is None:
        return _UNKNOWN
    old = observed_at is None or (now - observed_at).total_seconds() > STALE_S
    return Evidence(
        value=value,
        source=_source(provenance),
        observed_at=observed_at,
        status="stale" if old else "fresh",
        truncated=truncated,
    )


def _missing(error: ReadError) -> Evidence:
    return Evidence(
        value=None, source="live_forge", observed_at=None, status="unknown", error=error
    )


def _checklist(task: Task | None) -> Checklist:
    if task is None or task.continuation is None:
        return Checklist(
            state="unknown", total=0, pending_count=0, pending=[], omitted=0
        )
    items = task.continuation.checklist
    open_items = [i for i in items if i.status is not ItemStatus.DONE]
    shown = [
        PendingItem(
            id=i.id,
            text=_short(i.text)[0],
            status=i.status,
            source=_source(i.provenance),
        )
        for i in open_items[:LIST_CAP]
    ]
    return Checklist(
        state=task.continuation.checklist_state,
        total=len(items),
        pending_count=len(open_items),
        pending=shown,
        omitted=len(open_items) - len(shown),
    )


def _wip(task: Task | None, now: datetime) -> Evidence:
    wip = task.continuation.wip if task and task.continuation else None
    if wip is None:
        return _UNKNOWN
    text, cut = _short(wip.summary)
    return _stored(text, wip.provenance, wip.observed_at, now, cut)


def _transfer(task: Task | None, now: datetime) -> Evidence:
    """Checkpoint transfer evidence (task checkpoint 3.4): a checkpoint WIP ref, or
    the env_checkpoint_* reason a failed transfer blocked with."""
    wip = task.continuation.wip if task and task.continuation else None
    if task and wip and wip.ref and wip.ref.startswith("checkpoint:"):
        return _stored(wip.ref, wip.provenance, wip.observed_at, now)
    if task and (task.block_reason or "").startswith("env_checkpoint_"):
        return _stored(
            task.block_reason, ContinuationProvenance.ARGUS, task.updated_at, now
        )
    return _UNKNOWN


def _delivery(
    task: Task | None, now: datetime, ref: PrRef | None, live: Evidence
) -> DeliveryEvidence:
    prs = [
        a
        for a in (task.continuation.artifacts if task and task.continuation else [])
        if a.kind is ArtifactKind.PR
    ]
    newest = max(prs, key=lambda a: a.observed_at) if prs else None
    stored = _UNKNOWN
    if newest is not None and newest.state in _KNOWN_PR:
        stored = _stored(newest.state, newest.provenance, newest.observed_at, now)
    pr = ref.url if ref else _safe_url(newest.url) if newest else None
    headline, status = stored, stored.status
    if live.value is not None:
        newer = stored.observed_at is None or (
            live.observed_at is not None and live.observed_at >= stored.observed_at
        )
        headline = live if newer else stored
        known = stored.value is not None
        status = "conflict" if known and stored.value != live.value else "fresh"
    merged = headline.value == PrState.MERGED
    return DeliveryEvidence(
        status=status,
        pr=pr,
        headline=headline,
        stored=stored,
        live=live,
        flags=[MERGED_PENDING_REVIEW] if merged else [],
    )


def _safe_url(url: str | None) -> str | None:
    """Scheme, host and path only: no userinfo, query or fragment."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return redact(urlunsplit((parts.scheme, netloc, parts.path, "", "")))


async def _decisions(snap: Snapshot, ids: list[str | None]) -> dict[str, Decision]:
    wanted = [i for i in ids if i]
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    async with snap.conn.execute(
        f"{decisions.SELECT} WHERE d.id IN ({marks})",
        wanted,
    ) as cur:
        return {d.id: d for d in map(decisions.to_decision, await cur.fetchall())}


async def _read_forge(
    db: Database, checker: PrChecker, refs: dict[str, PrRef]
) -> tuple[dict[str, Evidence], int, int]:
    """Live PR states of at most FORGE_READS tasks, concurrently, within
    FORGE_BUDGET_S overall. Never raises; the error class is all a failure says."""
    chosen = list(refs.items())[:FORGE_READS]
    if not chosen:
        return {}, 0, 0
    out: dict[str, Evidence] = {}
    timed_out = 0
    async with db.read() as conn:
        jobs = {
            task_id: asyncio.ensure_future(checker.check(conn, ref))
            for task_id, ref in chosen
        }
        await asyncio.wait(jobs.values(), timeout=FORGE_BUDGET_S)
        for task_id, job in jobs.items():
            if not job.done():
                job.cancel()
                timed_out += 1
                out[task_id] = _missing("timeout")
                continue
            try:
                state, _, checked_at = job.result()
            except Exception:  # a failed read is unknown, never an error page
                out[task_id] = _missing("unavailable")
                continue
            if state is PrState.UNKNOWN:
                out[task_id] = _missing("unavailable")
            else:
                out[task_id] = Evidence(
                    value=state,
                    source="live_forge",
                    observed_at=checked_at,
                    status="fresh",
                )
        await asyncio.gather(*jobs.values(), return_exceptions=True)
    return out, len(chosen), timed_out


async def _no_longer_open(db: Database, ids: list[str]) -> set[str]:
    """Decisions of the page that were answered or cancelled after the snapshot."""
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    sql = f"SELECT id FROM decisions WHERE state != 'open' AND id IN ({marks})"  # noqa: S608
    async with db.read() as conn, conn.execute(sql, ids) as cur:
        return {r[0] for r in await cur.fetchall()}
