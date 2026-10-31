"""Task drafts and their publication (docs/specs/task-drafts.spec.md).

The one implementation behind REST and MCP. A draft is a row of `task_drafts`,
never of `tasks`, so nothing can claim it; publish creates every task of a batch
through `service.create_task` in the caller's single write transaction.
"""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.task_drafts import (
    MAX_OPEN_DRAFTS,
    PublishOutcome,
    PublishRequest,
    PublishResult,
    TaskDraft,
    TaskDraftBody,
    TaskDraftPage,
)
from argus_core.tasks import TaskCreate
from pydantic import ValidationError

from argus_api.db import Tx, ts
from argus_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.tasks import service

_TICK = timedelta(microseconds=1)
# Which refusal an atomic publish reports when several items fail (3.3).
_PRECEDENCE = ("not_found", "stale_version", "draft_published", "dependency_cycle")


# -- reads -------------------------------------------------------------------


async def get_draft(conn: aiosqlite.Connection, draft_id: str) -> TaskDraft:
    return _to_draft(await _row(conn, draft_id))


async def list_drafts(conn: aiosqlite.Connection) -> TaskDraftPage:
    async with conn.execute(
        "SELECT * FROM task_drafts WHERE published_at IS NULL ORDER BY created_at, id"
    ) as cur:
        items = [_to_draft(r) for r in await cur.fetchall()]
    return TaskDraftPage(items=items, total=len(items))


# -- open drafts -------------------------------------------------------------


async def create_draft(tx: Tx, body: TaskDraftBody, actor: str) -> TaskDraft:
    async with tx.conn.execute(
        "SELECT COUNT(*) FROM task_drafts WHERE published_at IS NULL"
    ) as cur:
        row = await cur.fetchone()
    if row is not None and row[0] >= MAX_OPEN_DRAFTS:
        raise ConflictError(
            f"at most {MAX_OPEN_DRAFTS} open drafts; publish or discard some first",
            code="too_many_drafts",
        )
    draft_id = str(uuid4())
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO task_drafts (id, body, created_by, created_at, updated_by,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (draft_id, _stored(body), actor, now, actor, now),
    )
    return await get_draft(tx.conn, draft_id)


async def edit_draft(
    tx: Tx, draft_id: str, changes: dict[str, Any], expected_version: str, actor: str
) -> TaskDraft:
    """Merge a partial patch into an open draft's body: `null` resets a field."""
    row = await _open_row(tx.conn, draft_id, expected_version)
    stored = json.loads(row["body"])
    merged = {k: v for k, v in {**stored, **changes}.items() if v is not None}
    try:
        body = TaskDraftBody.model_validate(merged)
    except ValidationError as exc:
        raise ValidationFailedError(
            "invalid draft: " + "; ".join(_errors(exc)), details=_errors(exc)
        ) from None
    if json.loads(_stored(body)) == stored:
        return _to_draft(row)  # nothing differs: no write, same version
    version = ts(max(tx.now, datetime.fromisoformat(row["updated_at"]) + _TICK))
    await tx.conn.execute(
        "UPDATE task_drafts SET body = ?, updated_by = ?, updated_at = ? WHERE id = ?",
        (_stored(body), actor, version, draft_id),
    )
    return await get_draft(tx.conn, draft_id)


async def discard_draft(tx: Tx, draft_id: str, expected_version: str) -> None:
    await _open_row(tx.conn, draft_id, expected_version)
    await tx.conn.execute("DELETE FROM task_drafts WHERE id = ?", (draft_id,))


# -- publication -------------------------------------------------------------


async def publish(
    tx: Tx, req: PublishRequest, actor: str, *, hourly_limit: int | None = None
) -> PublishResult:
    """Validate and publish a batch (3.3), idempotently by key (3.4)."""
    digest = _digest(req)
    async with tx.conn.execute(
        "SELECT request_digest, answer FROM task_draft_publications"
        " WHERE idempotency_key = ?",
        (req.idempotency_key,),
    ) as cur:
        seen = await cur.fetchone()
    if seen is not None:
        if seen["request_digest"] != digest:
            raise ConflictError(
                "this idempotency_key was used for another publish request",
                code="idempotency_key_reused",
            )
        answer = PublishResult.model_validate_json(seen["answer"])
        return answer.model_copy(update={"replayed": True})

    ids = [d.draft_id for d in req.drafts]
    rows = await _rows(tx.conn, ids)
    outcomes: dict[str, PublishOutcome] = {}
    bodies: dict[str, TaskDraftBody] = {}
    outside: dict[str, str] = {}  # published drafts outside the batch -> task id
    for item in req.drafts:
        row = rows.get(item.draft_id)
        if row is None:
            outcomes[item.draft_id] = _rejected(
                item.draft_id, "not_found", "draft not found"
            )
        elif row["published_at"] is not None:
            outcomes[item.draft_id] = _rejected(
                item.draft_id,
                "draft_published",
                f"already published as task {row['published_task_id']}",
            )
        elif row["updated_at"] != item.expected_version:
            outcomes[item.draft_id] = _rejected(
                item.draft_id,
                "stale_version",
                f"draft changed since you read it (version {row['updated_at']})",
            )
        else:
            bodies[item.draft_id] = TaskDraftBody.model_validate_json(row["body"])
    refs = {d for b in bodies.values() for d in b.depends_on_drafts} - set(ids)
    for ref, row in (await _rows(tx.conn, sorted(refs))).items():
        if row["published_task_id"] is not None:
            outside[ref] = row["published_task_id"]
    for draft_id, body in bodies.items():
        if why := _incomplete(body, outside, set(ids)):
            outcomes[draft_id] = _rejected(draft_id, "validation_failed", *why)

    order, cycles = _order(ids, {i: b.depends_on_drafts for i, b in bodies.items()})
    for draft_id, path in cycles.items():
        outcomes.setdefault(
            draft_id,
            _rejected(
                draft_id,
                "dependency_cycle",
                "these drafts depend on each other: " + " -> ".join(path),
            ),
        )

    task_ids: dict[str, str] = dict(outside)
    for draft_id in order:
        if draft_id in outcomes:
            continue
        body = bodies[draft_id]
        if waits := [d for d in body.depends_on_drafts if d not in task_ids]:
            outcomes[draft_id] = PublishOutcome(
                draft_id=draft_id,
                outcome="skipped",
                errors=[f"depends_on_drafts: {d} was not published" for d in waits],
            )
            continue
        create = TaskCreate.model_validate(
            {
                **body.model_dump(exclude_unset=True, exclude={"depends_on_drafts"}),
                "depends_on": [
                    *body.depends_on,
                    *(task_ids[d] for d in body.depends_on_drafts),
                ],
            }
        )
        try:
            async with tx.savepoint():
                task = await service.create_task(
                    tx, create, actor, hourly_limit=hourly_limit
                )
                await _check_capabilities(tx.conn, task.required_capabilities)
                await tx.conn.execute(
                    "UPDATE task_drafts SET published_at = ?, published_task_id = ?,"
                    " publication_key = ? WHERE id = ?",
                    (ts(tx.now), task.id, req.idempotency_key, draft_id),
                )
                await record(
                    tx,
                    actor,
                    "task_draft",
                    draft_id,
                    "task_draft.published",
                    {"task_id": task.id, "idempotency_key": req.idempotency_key},
                )
        except DomainError as exc:
            outcomes[draft_id] = _rejected(
                draft_id, exc.code, exc.message, *exc.details
            )
            continue
        task_ids[draft_id] = task.id
        outcomes[draft_id] = PublishOutcome(
            draft_id=draft_id, outcome="published", task_id=task.id, state=task.state
        )

    result = PublishResult(
        idempotency_key=req.idempotency_key,
        mode=req.mode,
        items=[outcomes[i] for i in ids],
    )
    failed = [o for o in result.items if o.outcome != "published"]
    if req.mode == "atomic" and failed:
        raise _atomic_refusal(failed)  # the write transaction rolls back
    await tx.conn.execute(
        "INSERT INTO task_draft_publications (idempotency_key, request_digest,"
        " answer, actor, created_at) VALUES (?, ?, ?, ?, ?)",
        (req.idempotency_key, digest, result.model_dump_json(), actor, ts(tx.now)),
    )
    return result


def _incomplete(
    body: TaskDraftBody, outside: dict[str, str], batch: set[str]
) -> list[str]:
    """Why a draft is not a complete task definition yet (3.3), or nothing."""
    try:
        TaskCreate.model_validate(
            body.model_dump(exclude_unset=True, exclude={"depends_on_drafts"})
        )
        errors = []
    except ValidationError as exc:
        errors = _errors(exc)
    if not body.spec.strip():
        errors.append("spec: a draft needs a spec to be published")
    if not body.acceptance:
        errors.append("acceptance: a draft needs at least one criterion")
    elif any(not a.strip() for a in body.acceptance):
        errors.append("acceptance: criteria cannot be blank")
    errors += [
        f"depends_on_drafts: {d} is not in this batch"
        for d in body.depends_on_drafts
        if d not in batch and d not in outside
    ]
    return errors


def _order(
    ids: list[str], edges: dict[str, list[str]]
) -> tuple[list[str], dict[str, list[str]]]:
    """The batch in dependency order, and each draft on an in-batch cycle with
    that cycle's path. Edges to drafts outside the batch are ignored here."""
    batch = set(ids)
    deps = {i: [d for d in edges.get(i, []) if d in batch] for i in ids}
    order: list[str] = []
    done: set[str] = set()
    while True:
        ready = [i for i in ids if i not in done and all(d in done for d in deps[i])]
        if not ready:
            break
        order += ready
        done.update(ready)
    cycles: dict[str, list[str]] = {}
    for start in ids:
        if start not in done and (path := _cycle_from(start, deps)):
            cycles[start] = path
    # Drafts left behind a cycle come last; they are skipped as not published.
    return order + [i for i in ids if i not in done], cycles


def _cycle_from(start: str, deps: dict[str, list[str]]) -> list[str] | None:
    """A path start -> ... -> start along dependency edges, if one exists."""
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    seen: set[str] = set()
    while stack:
        node, path = stack.pop()
        for dep in deps[node]:
            if dep == start:
                return [*path, start]
            if dep not in seen:
                seen.add(dep)
                stack.append((dep, [*path, dep]))
    return None


async def _check_capabilities(conn: aiosqlite.Connection, required: list[str]) -> None:
    """Each capability must be declared by a pool or a non-archived agent."""
    if not required:
        return
    async with conn.execute(
        "SELECT j.value FROM pools, json_each(pools.capabilities) AS j"
        " UNION SELECT j.value FROM agents, json_each(agents.capabilities) AS j"
        " WHERE agents.archived_at IS NULL"
    ) as cur:
        known = {r[0] for r in await cur.fetchall()}
    if unknown := [c for c in required if c not in known]:
        raise ValidationFailedError(
            "no pool or agent declares these capabilities: " + ", ".join(unknown),
            code="unknown_capability",
            details=[f"required_capabilities: {c}" for c in unknown],
        )


def _atomic_refusal(failed: list[PublishOutcome]) -> DomainError:
    codes = [o.code for o in failed if o.code]
    code = next((c for c in _PRECEDENCE if c in codes), codes[0] if codes else None)
    cls = {
        "not_found": NotFoundError,
        "stale_version": ConflictError,
        "draft_published": ConflictError,
    }.get(code or "", ValidationFailedError)
    details = [
        f"{o.draft_id}: {o.code or o.outcome}: {e}" for o in failed for e in o.errors
    ]
    return cls(
        f"nothing was published: {len(failed)} draft(s) cannot be published",
        code=code if code in _PRECEDENCE else "validation_failed",
        details=details,
    )


def _rejected(draft_id: str, code: str, *errors: str) -> PublishOutcome:
    return PublishOutcome(
        draft_id=draft_id, outcome="rejected", code=code, errors=list(errors)
    )


def _digest(req: PublishRequest) -> str:
    canonical = json.dumps(
        {
            "mode": req.mode,
            "drafts": [[d.draft_id, d.expected_version] for d in req.drafts],
        },
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# -- rows --------------------------------------------------------------------


def _stored(body: TaskDraftBody) -> str:
    # Only fields the author set: a kind's defaults still apply at publish.
    return json.dumps(body.model_dump(mode="json", exclude_unset=True), sort_keys=True)


def _errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(map(str, e['loc'])) or 'body'}: {e['msg']}" for e in exc.errors()
    ]


async def _row(conn: aiosqlite.Connection, draft_id: str) -> aiosqlite.Row:
    async with conn.execute(
        "SELECT * FROM task_drafts WHERE id = ?", (draft_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"draft not found: {draft_id}")
    return row


async def _rows(conn: aiosqlite.Connection, ids: list[str]) -> dict[str, aiosqlite.Row]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    sql = f"SELECT * FROM task_drafts WHERE id IN ({marks})"  # noqa: S608
    async with conn.execute(sql, ids) as cur:
        return {r["id"]: r for r in await cur.fetchall()}


async def _open_row(
    conn: aiosqlite.Connection, draft_id: str, expected_version: str
) -> aiosqlite.Row:
    row = await _row(conn, draft_id)
    if row["published_at"] is not None:
        raise ConflictError(
            f"draft {draft_id} was published as task {row['published_task_id']};"
            " edit the task instead",
            code="draft_published",
            details=[row["published_task_id"]],
        )
    if row["updated_at"] != expected_version:
        raise ConflictError(
            f"draft {draft_id} changed since you read it (version"
            f" {row['updated_at']}); read it again and reapply your change",
            code="stale_version",
            details=[row["updated_at"]],
        )
    return row


def _to_draft(row: aiosqlite.Row) -> TaskDraft:
    return TaskDraft(
        id=row["id"],
        body=TaskDraftBody.model_validate_json(row["body"]),
        version=row["updated_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
        published_at=row["published_at"],
        published_task_id=row["published_task_id"],
    )
