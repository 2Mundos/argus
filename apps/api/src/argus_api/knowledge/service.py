"""Projects, versioned knowledge pages, search, and proposals.

See docs/specs/knowledge.spec.md. Pages are authored Markdown; every change is a
new immutable revision published by compare-and-swap on the page's current
revision. The FTS index is updated in the same transaction as each publish.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from argus_core.decisions import Audience, DecisionKind
from argus_core.knowledge import (
    KnowledgeRef,
    Page,
    PageContent,
    PageCreate,
    PageEdit,
    PageListItem,
    PageRestore,
    Project,
    ProjectUpsert,
    Proposal,
    ProposalState,
    Revision,
    SaveToKnowledge,
    SearchHit,
    Source,
)
from argus_core.tasks import Task

from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import ConflictError, NotFoundError
from argus_api.events import record

DEFAULT_STALE_AFTER = timedelta(days=30)
REFS_ON_CLAIM = 5

# -- projects ----------------------------------------------------------------------


async def list_projects(conn: aiosqlite.Connection) -> list[Project]:
    async with conn.execute("SELECT * FROM projects ORDER BY slug") as cur:
        return [Project.model_validate(dict(r)) for r in await cur.fetchall()]


async def get_project(conn: aiosqlite.Connection, slug: str) -> Project:
    async with conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"unknown project: {slug}", code="unknown_project")
    return Project.model_validate(dict(row))


async def upsert_project(tx: Tx, slug: str, body: ProjectUpsert, actor: str) -> Project:
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO projects (slug, name, description, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (slug) DO UPDATE SET"
        " name = excluded.name, description = excluded.description,"
        " updated_at = excluded.updated_at",
        (slug, body.name, body.description, now, now),
    )
    await record(tx, actor, "project", slug, "project.upserted")
    return await get_project(tx.conn, slug)


# -- reading pages -------------------------------------------------------------------


async def get_page(
    conn: aiosqlite.Connection,
    page_id: str,
    *,
    now: datetime,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    revision: int | None = None,
) -> Page:
    page = await _page_row(conn, page_id)
    number = page["current_number"] if revision is None else revision
    async with conn.execute(
        "SELECT * FROM knowledge_revisions WHERE page_id = ? AND number = ?",
        (page_id, number),
    ) as cur:
        rev = await cur.fetchone()
    if rev is None:
        raise NotFoundError(f"page {page_id} has no revision {number}")
    return Page(
        id=page["id"],
        project=page["project"],
        slug=page["slug"],
        title=rev["title"],
        summary=rev["summary"],
        body=rev["body"],
        tags=json.loads(rev["tags"]),
        state=page["state"],
        revision_id=rev["id"],
        revision_number=rev["number"],
        author=rev["author"],
        reason=rev["reason"],
        sources=[Source.model_validate(s) for s in json.loads(rev["sources"])],
        stale=_stale(page["reviewed_at"], now, stale_after),
        reviewed_at=page["reviewed_at"],
        created_at=page["created_at"],
        updated_at=page["updated_at"],
    )


async def list_pages(
    conn: aiosqlite.Connection,
    *,
    now: datetime,
    project: str | None = None,
    include_archived: bool = False,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> list[PageListItem]:
    sql, params = "SELECT * FROM knowledge_pages WHERE 1 = 1", []
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    if not include_archived:
        sql += " AND state = 'active'"
    async with conn.execute(sql + " ORDER BY project, title", params) as cur:
        rows = await cur.fetchall()
    return [
        PageListItem(
            id=r["id"],
            project=r["project"],
            slug=r["slug"],
            title=r["title"],
            summary=r["summary"],
            tags=json.loads(r["tags"]),
            state=r["state"],
            revision_number=r["current_number"],
            stale=_stale(r["reviewed_at"], now, stale_after),
            reviewed_at=r["reviewed_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def list_revisions(conn: aiosqlite.Connection, page_id: str) -> list[Revision]:
    await _page_row(conn, page_id)
    async with conn.execute(
        "SELECT * FROM knowledge_revisions WHERE page_id = ? ORDER BY number DESC",
        (page_id,),
    ) as cur:
        return [_to_revision(r) for r in await cur.fetchall()]


# -- search ------------------------------------------------------------------------

_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "that",
        "this",
        "then",
        "than",
        "are",
        "was",
        "were",
        "has",
        "have",
        "not",
        "but",
        "you",
        "your",
        "our",
        "its",
        "all",
        "any",
        "can",
        "how",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
    ]
)


def _terms(text: str) -> list[str]:
    """Words worth matching, quoted so user input can never be FTS5 syntax."""
    words = [w.lower() for w in re.findall(r"\w+", text)]
    return [f'"{w}"' for w in dict.fromkeys(words) if len(w) > 2 and w not in _STOP]


async def search(
    conn: aiosqlite.Connection,
    q: str,
    *,
    now: datetime,
    project: str | None = None,
    limit: int = 20,
    match_any: bool = False,
    exclude: set[str] | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> list[SearchHit]:
    """Ranked full-text search over active pages. All terms must match unless
    `match_any`; stale pages rank after fresh ones but are never dropped."""
    terms = _terms(q)
    if not terms:
        return []
    sql = (
        "SELECT p.*, snippet(knowledge_fts, 3, '**', '**', ' … ', 16) AS snip"
        " FROM knowledge_fts JOIN knowledge_pages p ON p.id = knowledge_fts.page_id"
        " WHERE knowledge_fts MATCH ? AND p.state = 'active'"
    )
    params: list[Any] = [(" OR " if match_any else " ").join(terms)]
    if project is not None:
        sql += " AND p.project = ?"
        params.append(project)
    # Weights per column: page_id, title, summary, body, tags.
    sql += " ORDER BY bm25(knowledge_fts, 0.0, 10.0, 4.0, 1.0, 6.0) LIMIT ?"
    params.append(limit * 2 + len(exclude or ()))
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    hits = [
        SearchHit(
            page_id=r["id"],
            project=r["project"],
            slug=r["slug"],
            title=r["title"],
            summary=r["summary"],
            snippet=r["snip"],
            stale=_stale(r["reviewed_at"], now, stale_after),
        )
        for r in rows
        if r["id"] not in (exclude or ())
    ]
    hits.sort(key=lambda h: h.stale)  # stable: keeps bm25 order within groups
    return hits[:limit]


async def refs_for_task(
    conn: aiosqlite.Connection,
    task: Task,
    *,
    now: datetime,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    limit: int = REFS_ON_CLAIM,
) -> list[KnowledgeRef]:
    """Pinned pages first, then the best matches for the task's title in its
    project and in `global`."""
    async with conn.execute(
        "SELECT p.* FROM task_knowledge k JOIN knowledge_pages p ON p.id = k.page_id"
        " WHERE k.task_id = ? AND p.state = 'active' ORDER BY p.title",
        (task.id,),
    ) as cur:
        refs = [
            KnowledgeRef(
                page_id=r["id"],
                project=r["project"],
                title=r["title"],
                summary=r["summary"],
                stale=_stale(r["reviewed_at"], now, stale_after),
                why="pinned",
            )
            for r in await cur.fetchall()
        ]
    seen = {r.page_id for r in refs}
    for scope in dict.fromkeys([task.project or "global", "global"]):
        if len(refs) >= limit:
            break
        for hit in await search(
            conn,
            task.title,
            now=now,
            project=scope,
            limit=limit - len(refs),
            match_any=True,
            exclude=seen,
            stale_after=stale_after,
        ):
            refs.append(
                KnowledgeRef(
                    page_id=hit.page_id,
                    project=hit.project,
                    title=hit.title,
                    summary=hit.summary,
                    stale=hit.stale,
                    why="match",
                )
            )
            seen.add(hit.page_id)
    return refs[:limit]


# -- writing pages -------------------------------------------------------------------


async def create_page(tx: Tx, body: PageCreate, actor: str) -> str:
    await get_project(tx.conn, body.project)
    slug = body.slug or slugify(body.title)
    async with tx.conn.execute(
        "SELECT id FROM knowledge_pages WHERE project = ? AND slug = ?",
        (body.project, slug),
    ) as cur:
        if (existing := await cur.fetchone()) is not None:
            raise ConflictError(
                f"project {body.project} already has a page {slug!r}"
                f" ({existing['id']}); edit it instead",
                code="knowledge_slug_taken",
            )
    page_id, revision_id = str(uuid4()), str(uuid4())
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO knowledge_pages (id, project, slug, title, summary, tags,"
        " current_revision_id, current_number, state, reviewed_at, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?, ?)",
        (
            page_id,
            body.project,
            slug,
            body.title,
            body.summary,
            json.dumps(body.tags),
            revision_id,
            now,
            now,
            now,
        ),
    )
    await _insert_revision(tx, page_id, 1, body, actor, revision_id)
    await _index(tx, page_id, body)
    await record(
        tx, actor, "knowledge_page", page_id, "knowledge.created", {"title": body.title}
    )
    return page_id


async def edit_page(tx: Tx, page_id: str, edit: PageEdit, actor: str) -> None:
    """Publish a new revision if `base_revision_id` is still current (CAS)."""
    page = await _page_row(tx.conn, page_id)
    if page["state"] != "active":
        raise ConflictError(
            f"page {page_id} is archived; unarchive it first",
            code="knowledge_page_archived",
        )
    if page["current_revision_id"] != edit.base_revision_id:
        raise ConflictError(
            f"page {page_id} changed since revision {edit.base_revision_id}: it is now"
            f" at revision {page['current_number']}. Re-read it and apply your change"
            " again",
            code="knowledge_conflict",
        )
    number = page["current_number"] + 1
    revision_id = await _insert_revision(tx, page_id, number, edit, actor)
    now = ts(tx.now)
    await tx.conn.execute(
        "UPDATE knowledge_pages SET title = ?, summary = ?, tags = ?,"
        " current_revision_id = ?, current_number = ?, reviewed_at = ?,"
        " updated_at = ? WHERE id = ?",
        (
            edit.title,
            edit.summary,
            json.dumps(edit.tags),
            revision_id,
            number,
            now,
            now,
            page_id,
        ),
    )
    await _index(tx, page_id, edit)
    await record(
        tx,
        actor,
        "knowledge_page",
        page_id,
        "knowledge.edited",
        {"revision": number, "reason": edit.reason},
    )


async def restore(tx: Tx, page_id: str, body: PageRestore, actor: str) -> None:
    """A restore is a new revision with an old revision's content."""
    async with tx.conn.execute(
        "SELECT * FROM knowledge_revisions WHERE id = ? AND page_id = ?",
        (body.revision_id, page_id),
    ) as cur:
        old = await cur.fetchone()
    if old is None:
        raise NotFoundError(
            f"revision {body.revision_id} is not a revision of {page_id}"
        )
    await edit_page(
        tx,
        page_id,
        PageEdit(
            base_revision_id=body.base_revision_id,
            title=old["title"],
            summary=old["summary"],
            body=old["body"],
            tags=json.loads(old["tags"]),
            reason=f"restore revision {old['number']}: {body.reason}",
            sources=[Source.model_validate(s) for s in json.loads(old["sources"])],
        ),
        actor,
    )


async def set_archived(tx: Tx, page_id: str, archived: bool, actor: str) -> None:
    page = await _page_row(tx.conn, page_id)
    state = "archived" if archived else "active"
    if page["state"] == state:
        return
    await tx.conn.execute(
        "UPDATE knowledge_pages SET state = ?, updated_at = ? WHERE id = ?",
        (state, ts(tx.now), page_id),
    )
    if archived:
        await tx.conn.execute("DELETE FROM knowledge_fts WHERE page_id = ?", (page_id,))
    else:
        rev = await _current_revision(tx.conn, page_id)
        await _index(tx, page_id, _content(rev))
    await record(tx, actor, "knowledge_page", page_id, f"knowledge.{state}")


async def mark_reviewed(tx: Tx, page_id: str, actor: str) -> None:
    await _page_row(tx.conn, page_id)
    await tx.conn.execute(
        "UPDATE knowledge_pages SET reviewed_at = ? WHERE id = ?",
        (ts(tx.now), page_id),
    )
    await record(tx, actor, "knowledge_page", page_id, "knowledge.reviewed")


async def save_answer(
    tx: Tx,
    save: SaveToKnowledge,
    *,
    question: str,
    answer: str,
    decision_id: str,
    task_id: str,
    task_project: str | None,
    actor: str,
) -> str:
    """Keep an answered question as knowledge: append to a page, or create one."""
    sources = [
        Source(type="decision", ref=decision_id),
        Source(type="task", ref=task_id),
    ]
    if save.page_id is not None:
        rev = await _current_revision(tx.conn, save.page_id)
        content = _content(rev)
        await edit_page(
            tx,
            save.page_id,
            PageEdit(
                base_revision_id=rev["id"],
                title=content.title,
                summary=content.summary,
                tags=content.tags,
                body=f"{content.body.rstrip()}\n\n## {question}\n\n{answer}\n",
                reason="saved an answered question",
                sources=sources,
            ),
            actor,
        )
        return save.page_id
    title = (save.title or question).strip()[:200]
    return await create_page(
        tx,
        PageCreate(
            project=save.project or task_project or "global",
            title=title,
            summary=answer[:500],
            body=f"**Question:** {question}\n\n**Answer:** {answer}\n",
            reason="saved an answered question",
            sources=sources,
        ),
        actor,
    )


# -- proposals --------------------------------------------------------------------------


async def propose(
    tx: Tx,
    actor: str,
    *,
    content: PageContent,
    project: str,
    slug: str | None,
    page_id: str | None,
    base_revision_id: str | None,
) -> str:
    """Record a worker's proposed change and open a `knowledge` decision for it."""
    await get_project(tx.conn, project)
    if page_id is not None:
        page = await _page_row(tx.conn, page_id)
        project, slug = page["project"], page["slug"]
    proposal_id = str(uuid4())
    await tx.conn.execute(
        "INSERT INTO knowledge_proposals (id, page_id, project, slug, title, summary,"
        " body, tags, base_revision_id, author, reason, sources, state, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (
            proposal_id,
            page_id,
            project,
            slug or slugify(content.title),
            content.title,
            content.summary,
            content.body,
            json.dumps(content.tags),
            base_revision_id,
            actor,
            content.reason,
            json.dumps([s.model_dump() for s in content.sources]),
            ts(tx.now),
        ),
    )
    what = "a new page" if page_id is None else "an edit to"
    await decisions.open_decision(
        tx,
        subject_type="knowledge_proposal",
        subject_id=proposal_id,
        kind=DecisionKind.KNOWLEDGE,
        question=(
            f"Proposes {what} {content.title!r} in {project}."
            + (f" Reason: {content.reason}" if content.reason else "")
        ),
        options=["accept", "reject"],
        audience=Audience.ANY,
        requested_by=actor,
    )
    return proposal_id


async def resolve_proposal(
    tx: Tx, proposal_id: str, *, accept: bool, actor: str
) -> str | None:
    """Accept (publish, still CAS-checked) or reject. Returns the page id."""
    prop = await _proposal_row(tx.conn, proposal_id)
    if prop["state"] != ProposalState.OPEN:
        raise ConflictError(
            f"proposal {proposal_id} is {prop['state']}", code="proposal_not_open"
        )
    page_id: str | None = None
    if accept:
        content = _content(prop)
        if prop["page_id"] is None:
            page_id = await create_page(
                tx,
                PageCreate(
                    project=prop["project"], slug=prop["slug"], **content.model_dump()
                ),
                prop["author"],
            )
        else:
            page_id = prop["page_id"]
            await edit_page(
                tx,
                page_id,
                PageEdit(
                    base_revision_id=prop["base_revision_id"], **content.model_dump()
                ),
                prop["author"],
            )
    await tx.conn.execute(
        "UPDATE knowledge_proposals SET state = ?, page_id = coalesce(page_id, ?)"
        " WHERE id = ?",
        ("accepted" if accept else "rejected", page_id, proposal_id),
    )
    await record(
        tx,
        actor,
        "knowledge_proposal",
        proposal_id,
        f"knowledge.proposal_{'accepted' if accept else 'rejected'}",
        {"page_id": page_id},
    )
    return page_id


async def get_proposal(conn: aiosqlite.Connection, proposal_id: str) -> Proposal:
    return _to_proposal(await _proposal_row(conn, proposal_id))


async def list_proposals(
    conn: aiosqlite.Connection, state: ProposalState | None = ProposalState.OPEN
) -> list[Proposal]:
    sql, params = "SELECT * FROM knowledge_proposals", []
    if state is not None:
        sql += " WHERE state = ?"
        params.append(state)
    async with conn.execute(sql + " ORDER BY created_at", params) as cur:
        return [_to_proposal(r) for r in await cur.fetchall()]


# -- internals ----------------------------------------------------------------------------


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64].strip("-")
    return slug or "page"


def _stale(reviewed_at: str, now: datetime, stale_after: timedelta) -> bool:
    return now - datetime.fromisoformat(reviewed_at) > stale_after


async def _insert_revision(
    tx: Tx,
    page_id: str,
    number: int,
    content: PageContent,
    actor: str,
    revision_id: str | None = None,
) -> str:
    revision_id = revision_id or str(uuid4())
    await tx.conn.execute(
        "INSERT INTO knowledge_revisions (id, page_id, number, title, summary, body,"
        " tags, author, reason, sources, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            revision_id,
            page_id,
            number,
            content.title,
            content.summary,
            content.body,
            json.dumps(content.tags),
            actor,
            content.reason,
            json.dumps([s.model_dump() for s in content.sources]),
            ts(tx.now),
        ),
    )
    return revision_id


async def _index(tx: Tx, page_id: str, content: PageContent) -> None:
    await tx.conn.execute("DELETE FROM knowledge_fts WHERE page_id = ?", (page_id,))
    await tx.conn.execute(
        "INSERT INTO knowledge_fts (page_id, title, summary, body, tags)"
        " VALUES (?, ?, ?, ?, ?)",
        (page_id, content.title, content.summary, content.body, " ".join(content.tags)),
    )


async def _page_row(conn: aiosqlite.Connection, page_id: str) -> aiosqlite.Row:
    async with conn.execute(
        "SELECT * FROM knowledge_pages WHERE id = ?", (page_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"knowledge page not found: {page_id}")
    return row


async def _current_revision(conn: aiosqlite.Connection, page_id: str) -> aiosqlite.Row:
    page = await _page_row(conn, page_id)
    async with conn.execute(
        "SELECT * FROM knowledge_revisions WHERE id = ?", (page["current_revision_id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row


async def _proposal_row(conn: aiosqlite.Connection, proposal_id: str) -> aiosqlite.Row:
    async with conn.execute(
        "SELECT * FROM knowledge_proposals WHERE id = ?", (proposal_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"knowledge proposal not found: {proposal_id}")
    return row


def _content(row: aiosqlite.Row) -> PageContent:
    return PageContent(
        title=row["title"],
        summary=row["summary"],
        body=row["body"],
        tags=json.loads(row["tags"]),
        reason=row["reason"],
        sources=[Source.model_validate(s) for s in json.loads(row["sources"])],
    )


def _to_revision(row: aiosqlite.Row) -> Revision:
    data = dict(row)
    data["tags"] = json.loads(data["tags"])
    data["sources"] = json.loads(data["sources"])
    return Revision.model_validate(data)


def _to_proposal(row: aiosqlite.Row) -> Proposal:
    data = dict(row)
    data["tags"] = json.loads(data["tags"])
    data["sources"] = json.loads(data["sources"])
    return Proposal.model_validate(data)
