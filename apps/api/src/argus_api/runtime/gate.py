"""The claim gate (project runtime spec 3.5): a worker is not handed a task whose
project image is not usable on its host. Reads only, no Docker, no network."""

from dataclasses import dataclass

import aiosqlite
from argus_core.runtime import RuntimeSpec, RuntimeSpecError, recipe_digest, resolve


@dataclass(frozen=True)
class Gate:
    failed: bool  # durable failure (the task is blocked) rather than a wait
    reason: str


async def spec_of(conn: aiosqlite.Connection, project: str) -> tuple[RuntimeSpec, bool]:
    async with conn.execute(
        "SELECT spec FROM project_runtimes WHERE project = ?", (project,)
    ) as cur:
        row = await cur.fetchone()
    return (
        (RuntimeSpec.model_validate_json(row["spec"]), True)
        if row
        else (RuntimeSpec(), False)
    )


async def claim_gate(
    conn: aiosqlite.Connection, agent_id: str, project: str
) -> Gate | None:
    """None: claim it. An agent that never prepared is not gated: its first
    `prepare` creates the build and holds the task."""
    async with conn.execute(
        "SELECT * FROM runtime_hosts WHERE agent_id = ?", (agent_id,)
    ) as cur:
        host = await cur.fetchone()
    if host is None:
        return None
    spec, _ = await spec_of(conn, project)
    try:
        digest = recipe_digest(resolve(project, spec))
    except RuntimeSpecError:
        return None
    async with conn.execute(
        "SELECT state, reason FROM runtime_builds WHERE project = ? AND host_id = ? AND run_as = ?"
        " AND recipe_digest = ? AND base_id = ? ORDER BY updated_at DESC LIMIT 1",
        (project, host["host_id"], host["run_as"], digest, host["base_id"]),
    ) as cur:
        build = await cur.fetchone()
    if build is None or build["state"] == "ready":
        return None
    if build["state"] == "failed":
        return Gate(True, build["reason"] or "project runtime preparation failed")
    return Gate(False, f"project runtime is {build['state']}")
