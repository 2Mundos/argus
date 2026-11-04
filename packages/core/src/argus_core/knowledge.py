"""Projects and knowledge pages (docs/specs/knowledge.spec.md)."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from argus_core.fleet import validate_repository_url

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class ProjectUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    # The project's code, as an HTTPS owner/repository URL (not a secret). Left out
    # of a PUT it is kept; null clears it. Workers resolve it at claim.
    repository_url: str | None = None

    @field_validator("repository_url")
    @classmethod
    def repository_url_shape(cls, value: str | None) -> str | None:
        return None if value is None else validate_repository_url(value)


class ProjectRepositoryChange(BaseModel):
    """Change only a project's repository; name and description are untouched."""

    repository_url: str | None  # required: null clears it

    @field_validator("repository_url")
    @classmethod
    def repository_url_shape(cls, value: str | None) -> str | None:
        return None if value is None else validate_repository_url(value)


class Project(ProjectUpsert):
    slug: str
    created_at: datetime
    updated_at: datetime


class ProjectStats(BaseModel):
    """What Argus holds for one project, counted from its own tables (never
    estimated). `repository_url` is the one Argus resolves at claim and
    `repository_source` says how: `project` (its record), `pools` (every pool that
    names it agrees), `conflict` (pools name different repositories, so Argus picks
    none; a worker whose own pool names it may still use that), `invalid` (the
    stored value is unusable and no pool supplies one) or None (nothing names one)."""

    slug: str
    repository_url: str | None
    repository_source: Literal["project", "pools", "conflict", "invalid"] | None
    tasks: dict[str, int]  # live (unarchived) tasks by state; absent state: none
    archived_tasks: int
    last_task_activity_at: datetime | None  # newest task update; None: no tasks
    knowledge_pages: int  # active pages


class ProjectRepository(BaseModel):
    """The repository Argus resolves for one project, and why. `status`: `configured`
    (`repository_url` is what a worker clones), `unset` (nothing names one),
    `ambiguous` (pools name different repositories and the project has no record)
    or `invalid` (the stored value is unusable). `source` is as in `ProjectStats`.
    `detail` says what to do next; a stored value is never echoed when unusable,
    because it may carry a credential."""

    project: str
    status: Literal["configured", "unset", "ambiguous", "invalid"]
    repository_url: str | None
    repository_source: Literal["project", "pools", "conflict", "invalid"] | None
    detail: str


_REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")


class ProjectCheckoutRequest(BaseModel):
    """`ref`: the branch, tag or commit to base the work on; None is the
    repository's default branch."""

    ref: str | None = None

    @field_validator("ref")
    @classmethod
    def ref_shape(cls, value: str | None) -> str | None:
        if value is not None and (
            not _REF.fullmatch(value) or value.startswith("-") or ".." in value
        ):
            raise ValueError("ref must be a plain branch, tag or commit name")
        return value


class ProjectCheckout(BaseModel):
    """A checkout in progress: a read-only task whose workspace the worker channel
    prepares from the project's repository (project code availability spec)."""

    project: str
    repository_url: str
    ref: str | None
    task_id: str
    workspace: str  # relative to the worker's agent group dir, one per task
    branch: str
    access: Literal["read"] = "read"


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
