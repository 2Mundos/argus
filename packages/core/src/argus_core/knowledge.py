"""Projects and knowledge pages (docs/specs/knowledge.spec.md)."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class ProjectUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""


class Project(ProjectUpsert):
    slug: str
    created_at: datetime
    updated_at: datetime


class Source(BaseModel):
    """What justifies a revision: a task, decision, artifact, or URL."""

    type: Literal["task", "decision", "artifact", "url"]
    ref: str = Field(min_length=1, max_length=2000)


class PageContent(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=200_000)
    tags: list[str] = []
    reason: str = Field(default="", max_length=2000)
    sources: list[Source] = []


class PageCreate(PageContent):
    project: str = "global"
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)  # from title if unset


class PageEdit(PageContent):
    base_revision_id: str  # compare-and-swap: the revision this edit starts from


class PageRestore(BaseModel):
    revision_id: str
    base_revision_id: str
    reason: str = Field(min_length=1, max_length=2000)


class PageState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Page(BaseModel):
    """A page with its current revision's content."""

    id: str
    project: str
    slug: str
    title: str
    summary: str
    body: str
    tags: list[str]
    state: PageState
    revision_id: str
    revision_number: int
    author: str
    reason: str
    sources: list[Source]
    stale: bool
    reviewed_at: datetime
    created_at: datetime
    updated_at: datetime


class PageListItem(BaseModel):
    id: str
    project: str
    slug: str
    title: str
    summary: str
    tags: list[str]
    state: PageState
    revision_number: int
    stale: bool
    reviewed_at: datetime
    updated_at: datetime


class Revision(BaseModel):
    id: str
    page_id: str
    number: int
    title: str
    summary: str
    body: str
    tags: list[str]
    author: str
    reason: str
    sources: list[Source]
    created_at: datetime


class KnowledgeRef(BaseModel):
    """A pointer delivered with a claim or a search: enough to decide whether to read."""

    page_id: str
    project: str
    title: str
    summary: str
    stale: bool
    why: Literal["pinned", "match"]


class SearchHit(BaseModel):
    page_id: str
    project: str
    slug: str
    title: str
    summary: str
    snippet: str
    stale: bool


class ProposalState(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"  # moot: e.g. its decision was cancelled


class Proposal(BaseModel):
    id: str
    page_id: str | None
    project: str
    slug: str
    title: str
    summary: str
    body: str
    tags: list[str]
    base_revision_id: str | None
    author: str
    reason: str
    sources: list[Source]
    state: ProposalState
    created_at: datetime


class KnowledgeWrite(BaseModel):
    """Result of a create/edit: published directly, or proposed for review."""

    status: Literal["published", "proposed"]
    page: Page | None = None
    proposal: Proposal | None = None


class SaveToKnowledge(BaseModel):
    """When answering a question: keep the answer as knowledge too."""

    page_id: str | None = None  # append to this page…
    project: str | None = None  # …or create a page here (default: task's project)
    title: str | None = None  # title for a new page (default: the question)
