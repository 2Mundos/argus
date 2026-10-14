"""Task kind registry and JSON Schema validation (worker lifecycle 3.11)."""

import json
from typing import Any

import aiosqlite
from argus_core.kinds import TaskKind, TaskKindUpsert
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from argus_api.db import Tx, ts
from argus_api.errors import NotFoundError, ValidationFailedError
from argus_api.events import record

_JSON = ("input_schema", "result_schema", "default_capabilities")


async def get_kind(conn: aiosqlite.Connection, name: str) -> TaskKind:
    async with conn.execute("SELECT * FROM task_kinds WHERE name = ?", (name,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(
            f"unknown task kind: {name}; create it first or use 'generic'",
            code="unknown_kind",
        )
    return _to_kind(row)


async def list_kinds(conn: aiosqlite.Connection) -> list[TaskKind]:
    async with conn.execute("SELECT * FROM task_kinds ORDER BY name") as cur:
        return [_to_kind(r) for r in await cur.fetchall()]


async def upsert_kind(tx: Tx, name: str, body: TaskKindUpsert, actor: str) -> TaskKind:
    for field in ("input_schema", "result_schema"):
        schema = getattr(body, field)
        if schema is not None:
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise ValidationFailedError(
                    f"{field} is not a valid JSON Schema",
                    code="invalid_schema",
                    details=[exc.message],
                ) from exc
    now = ts(tx.now)
    values = (
        body.description,
        _dump(body.input_schema),
        _dump(body.result_schema),
        _dump(body.default_capabilities),
        body.default_lease_ttl_s,
        body.default_max_attempts,
        body.default_review,
    )
    await tx.conn.execute(
        "INSERT INTO task_kinds (name, description, input_schema, result_schema,"
        " default_capabilities, default_lease_ttl_s, default_max_attempts,"
        " default_review, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (name) DO UPDATE SET description = excluded.description,"
        " input_schema = excluded.input_schema, result_schema = excluded.result_schema,"
        " default_capabilities = excluded.default_capabilities,"
        " default_lease_ttl_s = excluded.default_lease_ttl_s,"
        " default_max_attempts = excluded.default_max_attempts,"
        " default_review = excluded.default_review, updated_at = excluded.updated_at",
        (name, *values, now, now),
    )
    await record(tx, actor, "task_kind", name, "task_kind.upserted")
    return await get_kind(tx.conn, name)


def validate(schema: dict[str, Any] | None, value: Any, what: str) -> None:
    """Raise ValidationFailedError listing every violation, if a schema is set."""
    if schema is None:
        return
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path)
    )
    if errors:
        raise ValidationFailedError(
            f"{what} does not match its kind's schema",
            details=[
                f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors
            ],
        )


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _to_kind(row: aiosqlite.Row) -> TaskKind:
    data = dict(row)
    for f in _JSON:
        if data[f] is not None:
            data[f] = json.loads(data[f])
    return TaskKind.model_validate(data)
