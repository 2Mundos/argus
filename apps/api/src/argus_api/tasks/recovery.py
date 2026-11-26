"""Where a task's worker left its work (MCP operations 3.2): per attempt the agent,
its instance, the workspace cleanup ledger row and the archive manifest summary,
plus the task's artifacts. Metadata only: no file bytes, and archive paths are read
only under the configured archive root."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiosqlite
from argus_core.redact import redact
from pydantic import BaseModel

from argus_api.errors import NotFoundError
from argus_api.fleet import workspace_archive as wa

MAX_ATTEMPTS = 50
MAX_LEDGER_ROWS = 5
MAX_ARTIFACTS = 100
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class ArchivedFile(BaseModel):
    name: str
    bytes: int
    sha256: str
    verified: bool  # the stored file still has this size and hash


class ArchivedRepo(BaseModel):
    rel: str
    branch: str | None
    head: str | None
    local_commits: int
    has_wip: bool
    staged_count: int
    unstaged_count: int
    untracked_count: int
    artifacts: list[ArchivedFile]


class ArchiveSummary(BaseModel):
    verified: bool  # every listed file re-hashed to its manifest value
    problem: str | None = None  # why it could not be read or verified
    repos: list[ArchivedRepo] = []


class LedgerRow(BaseModel):
    status: str
    reason: str
    archive_path: str | None
    archive: ArchiveSummary | None = None  # None when there is no archive


class AttemptRecovery(BaseModel):
    attempt: int
    agent_id: str
    instance_id: str | None
    claimed_at: str
    ledger: list[LedgerRow]


class RecoveryArtifact(BaseModel):
    name: str
    attempt: int
    role: str | None
    size: int
    sha256: str


class TaskRecovery(BaseModel):
    task_id: str
    attempts: list[AttemptRecovery]
    artifacts: list[RecoveryArtifact]


def _summarize(path: str, root: str | None) -> ArchiveSummary:
    """Blocking: reads the manifest and re-hashes the stored files."""
    if not root:
        return ArchiveSummary(verified=False, problem="archive_root_unset")
    real = os.path.realpath(path)
    base = os.path.realpath(root)
    if os.path.commonpath([real, base]) != base:
        return ArchiveSummary(verified=False, problem="outside_archive_root")
    directory = Path(real)
    try:
        manifest_path = directory / "manifest.json"
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            return ArchiveSummary(verified=False, problem="manifest_too_large")
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, ValueError):
        return ArchiveSummary(verified=False, problem="manifest_unreadable")
    writer = wa.ArchiveWriter()
    repos: list[ArchivedRepo] = []
    try:
        for r in manifest["repos"]:
            files = [
                ArchivedFile(
                    name=a["name"],
                    bytes=a["bytes"],
                    sha256=a["sha256"],
                    verified=_file_ok(directory, a, writer),
                )
                for a in r["artifacts"]
            ]
            repos.append(
                ArchivedRepo(
                    rel=redact(r["rel"]),
                    branch=redact(r["branch"]) if r["branch"] else None,
                    head=r["head"],
                    local_commits=r["local_commits"],
                    has_wip=r["has_wip"],
                    staged_count=r["staged_count"],
                    unstaged_count=r["unstaged_count"],
                    untracked_count=r["untracked_count"],
                    artifacts=files,
                )
            )
    except (KeyError, TypeError):
        return ArchiveSummary(verified=False, problem="manifest_invalid")
    verified = all(f.verified for r in repos for f in r.artifacts)
    return ArchiveSummary(verified=verified, repos=repos)


def _file_ok(directory: Path, entry: dict[str, Any], writer: wa.ArchiveWriter) -> bool:
    name = entry["name"]
    if not isinstance(name, str) or "/" in name or name.startswith("."):
        return False
    try:
        path = directory / name
        return (
            os.lstat(path).st_size == entry["bytes"]
            and writer.hash_file(path) == entry["sha256"]
        )
    except OSError:
        return False


async def get_recovery(
    conn: aiosqlite.Connection, task_id: str, archive_root: str | None
) -> TaskRecovery:
    async with conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)) as cur:
        if await cur.fetchone() is None:
            raise NotFoundError(f"task not found: {task_id}")
    async with conn.execute(
        "SELECT ta.attempt, ta.agent_id, ta.claimed_at, a.instance_id"
        " FROM task_attempts ta LEFT JOIN agents a ON a.id = ta.agent_id"
        " WHERE ta.task_id = ? ORDER BY ta.id LIMIT ?",
        (task_id, MAX_ATTEMPTS),
    ) as cur:
        attempts = list(await cur.fetchall())
    summaries: dict[str, ArchiveSummary] = {}
    out: list[AttemptRecovery] = []
    for at in attempts:
        ledger: list[LedgerRow] = []
        if at["instance_id"]:
            async with conn.execute(
                "SELECT status, reason, archive_path FROM workspace_cleanup"
                " WHERE instance_id = ? ORDER BY updated_at DESC LIMIT ?",
                (at["instance_id"], MAX_LEDGER_ROWS),
            ) as cur:
                rows = list(await cur.fetchall())
            for row in rows:
                archive = None
                if path := row["archive_path"]:
                    if path not in summaries:
                        summaries[path] = await asyncio.to_thread(
                            _summarize, path, archive_root
                        )
                    archive = summaries[path]
                ledger.append(
                    LedgerRow(
                        status=row["status"],
                        reason=redact(row["reason"] or ""),
                        archive_path=path,
                        archive=archive,
                    )
                )
        out.append(
            AttemptRecovery(
                attempt=at["attempt"],
                agent_id=at["agent_id"],
                instance_id=at["instance_id"],
                claimed_at=at["claimed_at"],
                ledger=ledger,
            )
        )
    async with conn.execute(
        "SELECT name, attempt, role, size, sha256 FROM artifacts"
        " WHERE task_id = ? AND (role IS NULL OR role != 'policy_snapshot')"
        " ORDER BY attempt, created_at, id LIMIT ?",
        (task_id, MAX_ARTIFACTS),
    ) as cur:
        artifacts = [
            RecoveryArtifact(**{**dict(r), "name": redact(r["name"])})
            for r in await cur.fetchall()
        ]
    return TaskRecovery(task_id=task_id, attempts=out, artifacts=artifacts)
