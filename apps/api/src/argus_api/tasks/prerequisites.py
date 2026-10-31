"""Read-only prerequisite evidence (docs/specs/task-discovery.spec.md).

Direct depends_on edges only. Distinguishes task_succeeded from forge-merged /
artifact-verified delivery. Never writes; never changes depends_on semantics.

The read snapshot is released before any network or disk work, exactly as
[task delivery] `delivery_of` is called outside a write transaction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import aiosqlite
import anyio
from argus_core.delivery import PrState
from argus_core.tasks import Task, TaskState
from pydantic import BaseModel, Field

from argus_api.artifacts import service as artifacts
from argus_api.artifacts.service import Artifact
from argus_api.artifacts.store import LocalArtifactStore
from argus_api.db import utcnow
from argus_api.errors import NotFoundError
from argus_api.tasks import delivery, service

MAX_PREREQUISITES = 25
MAX_ARTIFACTS = 3
ARTIFACT_BYTE_CAP = 8 * 1024 * 1024  # 8 MiB total across listed artifacts

Verdict = Literal["satisfied", "unsatisfied", "unknown"]
Verified = Literal[True, False, "unknown"]


class ArtifactEvidence(BaseModel):
    id: str
    name: str
    sha256: str
    size: int
    verified: Verified
    checked_at: datetime


class PrerequisiteEvidence(BaseModel):
    pr: str | None = None
    pr_state: PrState | None = None
    merged_at: datetime | None = None
    base_ref: str | None = None
    head_sha: str | None = None
    checked_at: datetime | None = None
    source: Literal["forge"] | None = None
    artifacts: list[ArtifactEvidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PrerequisiteItem(BaseModel):
    task_id: str
    title: str
    state: TaskState
    task_succeeded: bool
    verdict: Verdict
    why: str
    evidence: PrerequisiteEvidence


class PrerequisitesView(BaseModel):
    task_id: str
    version: str
    enforced: Literal[False] = False
    expected_base: str | None
    items: list[PrerequisiteItem]
    omitted: list[str] = Field(default_factory=list)
    partial: list[str] = Field(default_factory=list)


@dataclass
class _ArtSnap:
    id: str
    artifact: Artifact | None  # None: id listed but row missing


@dataclass
class _DepSnap:
    dep: Task
    ref: delivery.PrRef | None
    login: tuple[str, str] | None
    arts: list[_ArtSnap]


@dataclass
class Collected:
    """Plain in-memory snapshot for evaluate; no open DB connection."""

    task_id: str
    version: str
    expected_base: str | None
    omitted: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    deps: list[_DepSnap] = field(default_factory=list)


async def dependency_ids(conn: aiosqlite.Connection, task_id: str) -> list[str]:
    """Direct depends_on ids, deterministic order (depends_on_id)."""
    async with conn.execute(
        "SELECT depends_on_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_id",
        (task_id,),
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


async def collect(
    conn: aiosqlite.Connection,
    checker: delivery.PrChecker,
    task: Task,
    *,
    expected_base: str | None = None,
) -> Collected:
    """Gather dependency rows, PR refs, forge logins and artifact metadata."""
    deps = await dependency_ids(conn, task.id)
    omitted: list[str] = []
    partial: list[str] = []
    if len(deps) > MAX_PREREQUISITES:
        omitted.append("dependencies")
        partial.append("dependencies")
        deps = deps[:MAX_PREREQUISITES]
    snaps: list[_DepSnap] = []
    for dep_id in deps:
        dep = await service.get_task(conn, dep_id)
        ref = await delivery.reference(conn, dep)
        login = await checker._token(conn, ref.provider) if ref is not None else None
        arts = await _collect_arts(conn, dep)
        snaps.append(_DepSnap(dep=dep, ref=ref, login=login, arts=arts))
    return Collected(
        task_id=task.id,
        version=task.version,
        expected_base=expected_base,
        omitted=omitted,
        partial=partial,
        deps=snaps,
    )


async def evaluate(
    checker: delivery.PrChecker,
    store: LocalArtifactStore,
    collected: Collected,
) -> PrerequisitesView:
    """Forge inspect and capped artifact re-hash; no open read connection."""
    items: list[PrerequisiteItem] = []
    partial = list(collected.partial)
    for snap in collected.deps:
        item, item_partial = await _item_from_snap(
            checker, store, snap, collected.expected_base
        )
        items.append(item)
        for name in item_partial:
            if name not in partial:
                partial.append(name)
    return PrerequisitesView(
        task_id=collected.task_id,
        version=collected.version,
        expected_base=collected.expected_base,
        items=items,
        omitted=list(collected.omitted),
        partial=partial,
    )


async def prerequisites_of(
    conn: aiosqlite.Connection,
    checker: delivery.PrChecker,
    store: LocalArtifactStore,
    task: Task,
    *,
    expected_base: str | None = None,
) -> PrerequisitesView:
    """Collect then evaluate. Callers that hold a pool reader must only run
    `collect` under it and call `evaluate` after the snapshot closes."""
    collected = await collect(conn, checker, task, expected_base=expected_base)
    return await evaluate(checker, store, collected)


async def _item_from_snap(
    checker: delivery.PrChecker,
    store: LocalArtifactStore,
    snap: _DepSnap,
    expected_base: str | None,
) -> tuple[PrerequisiteItem, list[str]]:
    dep = snap.dep
    succeeded = dep.state is TaskState.SUCCEEDED
    evidence = PrerequisiteEvidence()
    partial: list[str] = []
    art_ev, art_why, art_partial = await _eval_arts(store, snap.arts)
    evidence.artifacts = art_ev
    partial += art_partial

    pr_why: str | None = None
    pr_verdict: Verdict | None = None
    ref = snap.ref
    if ref is not None:
        obs = await checker.inspect_resolved(ref, snap.login)
        evidence.pr = ref.url
        evidence.pr_state = obs.state
        evidence.merged_at = obs.merged_at
        evidence.base_ref = obs.base_ref
        evidence.head_sha = obs.head_sha
        evidence.checked_at = obs.checked_at
        evidence.source = "forge"
        if obs.state is PrState.UNKNOWN:
            pr_verdict, pr_why = "unknown", "forge_unavailable"
            partial.append("forge")
        elif obs.state is PrState.MERGED:
            if expected_base is not None and obs.base_ref != expected_base:
                pr_verdict, pr_why = "unsatisfied", "base_mismatch"
            else:
                pr_verdict, pr_why = "satisfied", "merged"
                evidence.notes.append("review_state_is_not_delivery_proof")
        elif obs.state is PrState.CLOSED:
            pr_verdict, pr_why = "unsatisfied", "closed_without_merge"
        elif obs.state is PrState.OPEN:
            pr_verdict, pr_why = "unsatisfied", "open"
        else:
            pr_verdict, pr_why = "unknown", "forge_unavailable"
            partial.append("forge")

    # Artifact channel: verified true wins; missing/corrupt unsatisfied; else unknown.
    art_verdict: Verdict | None = None
    if any(a.verified is True for a in art_ev):
        art_verdict, art_why = "satisfied", "artifact_verified"
    elif art_why in ("artifact_missing", "artifact_corrupted"):
        art_verdict = "unsatisfied"
    elif art_ev and art_why:
        art_verdict = "unknown"
    elif not art_ev:
        art_why = None

    verdict, why = _combine(pr_verdict, pr_why, art_verdict, art_why, ref is not None)
    return (
        PrerequisiteItem(
            task_id=dep.id,
            title=dep.title,
            state=dep.state,
            task_succeeded=succeeded,
            verdict=verdict,
            why=why,
            evidence=evidence,
        ),
        partial,
    )


def _combine(
    pr_verdict: Verdict | None,
    pr_why: str | None,
    art_verdict: Verdict | None,
    art_why: str | None,
    has_pr: bool,
) -> tuple[Verdict, str]:
    if pr_verdict == "satisfied" or art_verdict == "satisfied":
        if pr_verdict == "satisfied":
            return "satisfied", pr_why or "merged"
        return "satisfied", art_why or "artifact_verified"
    if has_pr and pr_verdict is not None and pr_why is not None:
        return pr_verdict, pr_why
    if art_verdict is not None and art_why is not None:
        return art_verdict, art_why
    return "unknown", "no_reference"


async def _collect_arts(conn: aiosqlite.Connection, dep: Task) -> list[_ArtSnap]:
    ids = list(dep.result.artifacts) if dep.result else []
    ids = sorted(ids)[:MAX_ARTIFACTS]
    out: list[_ArtSnap] = []
    for aid in ids:
        try:
            art = await artifacts.get_artifact(conn, aid)
        except NotFoundError:
            out.append(_ArtSnap(id=aid, artifact=None))
            continue
        out.append(_ArtSnap(id=aid, artifact=art))
    return out


async def _eval_arts(
    store: LocalArtifactStore, snaps: list[_ArtSnap]
) -> tuple[list[ArtifactEvidence], str | None, list[str]]:
    """Bounded re-hash from collected metadata; no DB access."""
    if not snaps:
        return [], None, []
    out: list[ArtifactEvidence] = []
    why: str | None = None
    partial: list[str] = []
    budget = ARTIFACT_BYTE_CAP
    for snap in snaps:
        art = snap.artifact
        if art is None:
            partial.append(f"artifact:{snap.id}")
            why = why or "artifact_missing"
            checked = utcnow()
            out.append(
                ArtifactEvidence(
                    id=snap.id,
                    name="",
                    sha256="",
                    size=0,
                    verified=False,
                    checked_at=checked,
                )
            )
            continue
        checked = utcnow()
        path = store.path(artifacts.key(art.task_id, art.attempt, art.name))
        verified: Verified
        if not path.is_file():
            verified = False
            why = "artifact_missing"
        elif art.size > budget:
            # Over remaining byte cap: size-only, never claim hash proof.
            verified = "unknown"
            why = why or "artifact_unverified"
            partial.append(f"artifact:{art.id}")
        else:
            verified, mismatch = await _rehash_capped(
                path, art.sha256, art.size, budget
            )
            budget -= min(art.size, budget)
            if verified is True:
                pass
            elif mismatch:
                verified = False
                why = "artifact_corrupted"
            else:
                verified = "unknown"
                why = why or "artifact_unverified"
                partial.append(f"artifact:{art.id}")
        out.append(
            ArtifactEvidence(
                id=art.id,
                name=art.name,
                sha256=art.sha256,
                size=art.size,
                verified=verified,
                checked_at=checked,
            )
        )
    return out, why, partial


async def _rehash_capped(
    path: Path, expected_sha: str, expected_size: int, budget: int
) -> tuple[Verified, bool]:
    """Return (verified, mismatch). mismatch True only when bytes disagree."""
    digest, size = hashlib.sha256(), 0
    try:
        async with await anyio.open_file(path, "rb") as f:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > budget:
                    return "unknown", False
                digest.update(chunk)
    except OSError:
        return "unknown", False
    if size != expected_size or digest.hexdigest() != expected_sha:
        return False, True
    return True, False
