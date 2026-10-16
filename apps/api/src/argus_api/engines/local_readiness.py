"""Local `validated` readiness from persisted `engine.local_run` events (engines spec)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from argus_core.engines import Fact

from argus_api.db import Database

_KIND = "engine.local_run"


def local_identity(endpoint: str, model: str, n_ctx: int | None) -> dict[str, Any]:
    return {"endpoint": endpoint, "model": model, "n_ctx": n_ctx}


def identities_match(stored: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if not stored:
        return False
    return (
        stored.get("endpoint") == current.get("endpoint")
        and stored.get("model") == current.get("model")
        and stored.get("n_ctx") == current.get("n_ctx")
    )


def validated_from_ledger(
    ledger_rec: dict[str, Any], identity: dict[str, Any]
) -> Fact | None:
    """Smoke may set `validation_identity` beside `validated`; legacy rows without it are ignored."""
    if not identities_match(ledger_rec.get("validation_identity"), identity):
        return None
    raw = ledger_rec.get("validated") or {}
    try:
        fact = Fact.model_validate(raw)
    except ValueError:
        return None
    return fact if fact.ok is True else None


async def validated_from_events(db: Database, identity: dict[str, Any]) -> Fact | None:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT at, data FROM events WHERE kind = ? ORDER BY id DESC LIMIT 500",
            (_KIND,),
        ) as cur,
    ):
        rows = await cur.fetchall()
    for row in rows:
        data = json.loads(row["data"])
        if data.get("verdict") != "accepted":
            continue
        if not data.get("criteria_effective"):
            continue
        if not identities_match(data.get("validation_identity"), identity):
            continue
        at = row["at"]
        if not isinstance(at, datetime):
            at = None
        return Fact(
            ok=True,
            detail="accepted local run with effective criteria",
            at=at,
        )
    return None


def merge_local_validated(
    ledger_rec: dict[str, Any],
    identity: dict[str, Any],
    event_fact: Fact | None,
) -> Fact:
    if event_fact is not None and event_fact.ok is True:
        return event_fact
    ledger_fact = validated_from_ledger(ledger_rec, identity)
    if ledger_fact is not None:
        return ledger_fact
    return Fact(
        ok=None,
        detail="no accepted local run for this endpoint, model and context",
    )
