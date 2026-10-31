"""Routine store: validation on save, persistence, schedule previews
(docs/specs/routines.spec.md 3.1 and 3.4). Runs are in `runs`."""

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.routines import (
    INTAKE_FIELDS,
    TEMPLATE_FIELDS,
    FindingIntake,
    Routine,
    RoutineRun,
    RoutineWrite,
)
from argus_core.tasks import TaskCreate
from pydantic import ValidationError

from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import ForbiddenError, NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.knowledge import service as knowledge
from argus_api.routines.schedule import (
    ScheduleError,
    next_slots,
    parse_schedule,
    parse_timezone,
)
from argus_api.tasks import service as tasks

PREVIEW_SLOTS = 3
LAST_RUNS = 5


# -- reads -------------------------------------------------------------------------


async def list_routines(
    conn: aiosqlite.Connection, project: str, now: datetime
) -> list[Routine]:
    await knowledge.get_project(conn, project)  # raises unknown_project
    async with conn.execute(
        "SELECT * FROM routines WHERE project = ? ORDER BY slug", (project,)
    ) as cur:
        return [_to_routine(r, now) for r in await cur.fetchall()]


async def get_routine(
    conn: aiosqlite.Connection, routine_id: str, now: datetime
) -> Routine:
    async with conn.execute(
        "SELECT * FROM routines WHERE id = ?", (routine_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"routine not found: {routine_id}")
    # Run tasks name their routine in `created_by` (routines spec 3.2).
    async with conn.execute(
        "SELECT id, state, created_at FROM tasks WHERE created_by = ?"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (f"routine:{routine_id}", LAST_RUNS),
    ) as cur:
        run_rows = list(await cur.fetchall())
    # Intake files children of the run as `system`; dedup-suppressed entries are
    # not children, and tasks an agent created with the same input do not count.
    async with conn.execute(
        "SELECT parent_id, COUNT(*) AS n FROM tasks"
        " WHERE parent_id IN (SELECT value FROM json_each(?))"
        " AND created_by = 'system'"
        " AND json_extract(input, '$.routine_finding.routine') = ?"
        " GROUP BY parent_id",
        (json.dumps([r["id"] for r in run_rows]), routine_id),
    ) as cur:
        filed = {r["parent_id"]: r["n"] for r in await cur.fetchall()}
    runs = [
        RoutineRun(
            task_id=r["id"],
            state=r["state"],
            created_at=datetime.fromisoformat(r["created_at"]),
            findings_filed=filed.get(r["id"], 0),
        )
        for r in run_rows
    ]
    return _to_routine(row, now).model_copy(update={"last_runs": runs})


# -- writes ------------------------------------------------------------------------


async def upsert(
    tx: Tx,
    project: str,
    slug: str,
    body: RoutineWrite,
    actor: str,
    *,
    admin: bool,
) -> Routine:
    """Create or replace the routine `project`/`slug`. A replacement keeps its id,
    creator, creation time and run history; `next_run_at` is recomputed."""
    mismatched = [
        f"{field}: {given!r} does not match the path ({path!r})"
        for field, given, path in (
            ("project", body.project, project),
            ("slug", body.slug, slug),
        )
        if given is not None and given != path
    ]
    if mismatched:
        raise ValidationFailedError(
            "the body names another routine than the path",
            code="identity_mismatch",
            details=mismatched,
        )
    await knowledge.get_project(tx.conn, project)  # raises unknown_project
    existing = await _row_by_slug(tx.conn, project, slug)
    _require_auto_admin(
        admin, body.findings, existing["findings"] if existing else None
    )
    schedule = body.schedule.strip()
    timezone, first_slot = _check_schedule(schedule, body.timezone, tx.now)
    template = await _check_template(tx.conn, project, body.title, body.template)
    values = {
        "title": body.title,
        "schedule": schedule,
        "timezone": timezone,
        "template": json.dumps(template),
        "findings": body.findings,
        "enabled": int(body.enabled),
        "next_run_at": ts(first_slot) if body.enabled else None,
        "updated_at": ts(tx.now),
    }
    if existing is None:
        routine_id = str(uuid4())
        await tx.conn.execute(
            "INSERT INTO routines (id, project, slug, title, schedule, timezone,"
            " template, findings, enabled, next_run_at, created_by, created_at,"
            " updated_at) VALUES (:id, :project, :slug, :title, :schedule,"
            " :timezone, :template, :findings, :enabled, :next_run_at, :created_by,"
            " :updated_at, :updated_at)",
            values
            | {"id": routine_id, "project": project, "slug": slug, "created_by": actor},
        )
        kind = "routine.created"
    else:
        routine_id = existing["id"]
        await tx.conn.execute(
            "UPDATE routines SET title = :title, schedule = :schedule,"
            " timezone = :timezone, template = :template, findings = :findings,"
            " enabled = :enabled, next_run_at = :next_run_at,"
            " updated_at = :updated_at WHERE id = :id",
            values | {"id": routine_id},
        )
        kind = "routine.updated"
    # Template text stays out of the event log (it is streamed and shown).
    await record(
        tx,
        actor,
        "routine",
        routine_id,
        kind,
        {
            "project": project,
            "slug": slug,
            "schedule": schedule,
            "timezone": timezone,
            "findings": body.findings,
            "enabled": body.enabled,
        },
    )
    if existing is not None and existing["enabled"] and not body.enabled:
        await _record_disabled(tx, actor, routine_id)
    if body.enabled:
        await _close_reenable_question(tx, routine_id)
    return await get_routine(tx.conn, routine_id, tx.now)


async def set_enabled(
    tx: Tx, project: str, slug: str, enabled: bool, actor: str, *, admin: bool
) -> Routine:
    """Disabling is always allowed to managers; enabling an `auto` routine is not."""
    row = await _require_by_slug(tx.conn, project, slug)
    if enabled:
        _require_auto_admin(admin, row["findings"])
    if bool(row["enabled"]) != enabled:
        next_run_at = None
        if enabled:
            schedule = parse_schedule(row["schedule"])
            tz = parse_timezone(row["timezone"])
            next_run_at = ts(next_slots(schedule, tz, tx.now)[0])
        await tx.conn.execute(
            "UPDATE routines SET enabled = ?, next_run_at = ?, updated_at = ?"
            " WHERE id = ?",
            (int(enabled), next_run_at, ts(tx.now), row["id"]),
        )
        if enabled:
            await record(
                tx, actor, "routine", row["id"], "routine.updated", {"enabled": True}
            )
            await _close_reenable_question(tx, row["id"])
        else:
            await _record_disabled(tx, actor, row["id"])
    return await get_routine(tx.conn, row["id"], tx.now)


async def reenable(tx: Tx, routine_id: str, actor: str) -> None:
    """An admin approved the re-enable question opened by the failure threshold
    (routines spec 3.5). A routine deleted since is left alone."""
    async with tx.conn.execute(
        "SELECT project, slug FROM routines WHERE id = ?", (routine_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        await set_enabled(tx, row["project"], row["slug"], True, actor, admin=True)


async def delete(tx: Tx, project: str, slug: str, actor: str) -> None:
    """Remove the routine. Its run tasks stay: they only name it in `created_by`."""
    row = await _require_by_slug(tx.conn, project, slug)
    await tx.conn.execute("DELETE FROM routines WHERE id = ?", (row["id"],))
    await _close_reenable_question(tx, row["id"])
    await record(
        tx,
        actor,
        "routine",
        row["id"],
        "routine.deleted",
        {"project": project, "slug": slug},
    )


# -- helpers -----------------------------------------------------------------------


def _require_auto_admin(admin: bool, *findings: str | None) -> None:
    """`auto` files untrusted findings straight to `ready` (routines spec 3.3): only
    an admin may set it or change a routine that has it."""
    if "auto" in findings and not admin:
        raise ForbiddenError(
            "findings 'auto' is admin only: only an admin may set it or change"
            " a routine that has it",
            code="admin_required",
        )


def _check_schedule(
    schedule: str, timezone: str, now: datetime
) -> tuple[str, datetime]:
    """(the canonical IANA name, the first slot after `now`). An expression that
    never fires within the look-ahead is refused too."""
    try:
        parsed = parse_schedule(schedule)
    except ScheduleError as exc:
        raise ValidationFailedError(
            f"invalid schedule: {exc}", code="invalid_schedule"
        ) from exc
    try:
        tz = parse_timezone(timezone)
    except ScheduleError as exc:
        raise ValidationFailedError(
            f"invalid timezone: {exc}", code="invalid_timezone"
        ) from exc
    try:
        first = next_slots(parsed, tz, now)[0]
    except ScheduleError as exc:
        raise ValidationFailedError(
            f"invalid schedule: {exc}", code="invalid_schedule"
        ) from exc
    return tz.key, first


async def _check_template(
    conn: aiosqlite.Connection, project: str, title: str, template: dict[str, Any]
) -> dict[str, Any]:
    """The template, normalised, after the task create rules (routines spec 3.1).
    Its title defaults to the routine's for the check; only given keys are kept."""
    if unknown := sorted(set(template) - TEMPLATE_FIELDS):
        raise ValidationFailedError(
            "unknown template fields: " + ", ".join(unknown),
            code="unknown_template_field",
            details=[f"template.{key}: not a template field" for key in unknown],
        )
    task_fields = {k: v for k, v in template.items() if k not in INTAKE_FIELDS}
    try:
        intake = FindingIntake.model_validate(template)
        body = TaskCreate.model_validate(
            {"title": title} | task_fields | {"project": project}
        )
    except ValidationError as exc:
        errors = [
            f"template.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        ]
        raise ValidationFailedError(
            "invalid template: " + "; ".join(errors), details=errors
        ) from exc
    await tasks.check_create(conn, body)
    return body.model_dump(mode="json", include=set(task_fields)) | intake.model_dump(
        include=set(template) & INTAKE_FIELDS
    )


async def _record_disabled(tx: Tx, actor: str, routine_id: str) -> None:
    await record(
        tx, actor, "routine", routine_id, "routine.disabled", {"reason": "manual"}
    )


async def _close_reenable_question(tx: Tx, routine_id: str) -> None:
    """The failure threshold's re-enable question is moot once a manager enables
    or deletes the routine."""
    await decisions.cancel_open(tx, "routine", routine_id)


async def _row_by_slug(
    conn: aiosqlite.Connection, project: str, slug: str
) -> aiosqlite.Row | None:
    async with conn.execute(
        "SELECT * FROM routines WHERE project = ? AND slug = ?", (project, slug)
    ) as cur:
        return await cur.fetchone()


async def _require_by_slug(
    conn: aiosqlite.Connection, project: str, slug: str
) -> aiosqlite.Row:
    row = await _row_by_slug(conn, project, slug)
    if row is None:
        raise NotFoundError(f"routine not found: {project}/{slug}")
    return row


def _to_routine(row: aiosqlite.Row, now: datetime) -> Routine:
    slots = next_slots(
        parse_schedule(row["schedule"]),
        parse_timezone(row["timezone"]),
        now,
        PREVIEW_SLOTS,
    )
    return Routine(
        id=row["id"],
        project=row["project"],
        slug=row["slug"],
        title=row["title"],
        schedule=row["schedule"],
        timezone=row["timezone"],
        template=json.loads(row["template"]),
        findings=row["findings"],
        enabled=bool(row["enabled"]),
        next_run_at=_at(row["next_run_at"]),
        last_run_at=_at(row["last_run_at"]),
        last_task_id=row["last_task_id"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        next_slots=slots,
    )


def _at(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)
