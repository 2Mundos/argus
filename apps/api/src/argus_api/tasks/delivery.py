"""Pull-request state of a task's delivery (docs/specs/task-delivery.spec.md).

Read-only. References are taken only from structured artifacts and unambiguous
text; a bare number needs the repository the project proves. The forge call (Gitea, or
GitHub for github.com) uses the registered account of that provider, is bounded by a short timeout, is coalesced per PR and
never runs inside a write transaction. Any failure reads as `unknown`, never `open`.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
import httpx2 as httpx
from argus_core.accounts import repository_provider
from argus_core.continuation import ArtifactKind, ContinuationArtifact
from argus_core.delivery import MERGED_PENDING_REVIEW, AcceptBlocked, Delivery, PrState
from argus_core.tasks import Task, TaskState

from argus_api.db import utcnow
from argus_api.fleet import service as fleet
from argus_api.secrets import SecretStore, ref_name

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0
CACHE_S = 15.0
TEXT_CAP = 20_000

_URL = re.compile(
    r"https://[^\s/<>()\[\]\"']+/[^\s/<>()\[\]\"']+/[^\s/<>()\[\]\"']+/pulls?/(\d{1,9})(?![\w/])"
)
_BARE = re.compile(r"\b(?:PR|pull request) #(\d{1,9})\b", re.IGNORECASE)


@dataclass(frozen=True)
class PrRef:
    host: str
    owner: str
    repo: str
    number: int

    @property
    def provider(self) -> str:
        return repository_provider(self.host)

    @property
    def url(self) -> str:
        pulls = "pull" if self.provider == "github" else "pulls"
        return f"https://{self.host}/{self.owner}/{self.repo}/{pulls}/{self.number}"


def parse_repository(url: str) -> tuple[str, str, str] | None:
    """`https://host/owner/repo[.git]` -> lowercase (host, owner, repo)."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if parts.scheme != "https" or not parts.hostname or len(segs) != 2:
        return None
    repo = segs[1].removesuffix(".git")
    return parts.hostname.lower(), segs[0].lower(), repo.lower()


def _from_url(text: str, allowed: set[tuple[str, str, str]]) -> list[PrRef]:
    out = []
    for m in _URL.finditer(text):
        parts = urlsplit(m.group(0))
        segs = [s for s in parts.path.split("/") if s]
        repo = (parts.hostname or "").lower(), segs[0].lower(), segs[1].lower()
        if repo in allowed:  # a token is never sent to a host nobody configured
            out.append(PrRef(*repo, int(m.group(1))))
    return out


def extract(
    texts: list[str],
    artifacts: list[ContinuationArtifact],
    project_repo: tuple[str, str, str] | None,
    allowed: set[tuple[str, str, str]],
) -> PrRef | None:
    """The one PR the task delivers, or None. Structured PR artifacts win (newest
    observation); otherwise the text must name exactly one distinct PR. Several
    different PRs without a structured one are ambiguous: nothing is guessed."""
    structured: list[tuple[datetime, PrRef]] = []
    for a in artifacts:
        if a.kind is not ArtifactKind.PR:
            continue
        found = _from_url(a.url or "", allowed)
        if not found and project_repo and a.ref.isdigit():
            found = [PrRef(*project_repo, int(a.ref))]
        structured += [(a.observed_at, r) for r in found[:1]]
    if structured:
        return max(structured, key=lambda s: s[0])[1]
    refs: set[PrRef] = set()
    for text in texts:
        text = text[:TEXT_CAP]
        refs.update(_from_url(text, allowed))
        if project_repo:
            refs.update(PrRef(*project_repo, int(n)) for n in _BARE.findall(text))
    return next(iter(refs)) if len(refs) == 1 else None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


async def repositories(conn: aiosqlite.Connection) -> dict[str, tuple[str, str, str]]:
    """project -> repository, from the pools' explicit allowlist. A project the pools
    disagree on is dropped: the repository is not proven."""
    found: dict[str, set[tuple[str, str, str]]] = {}
    for pool in await fleet.list_pools(conn):
        if pool.nanoclaw is None:
            continue
        for project, url in pool.nanoclaw.repositories.items():
            if repo := parse_repository(url):
                found.setdefault(project, set()).add(repo)
    return {p: next(iter(r)) for p, r in found.items() if len(r) == 1}


async def reference(conn: aiosqlite.Connection, task: Task) -> PrRef | None:
    repos = await repositories(conn)
    texts = []
    if task.result:
        texts += [task.result.summary, *_strings(task.result.data)]
    texts += [task.progress_message or "", *_strings(task.progress_data)]
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " ORDER BY id DESC LIMIT 50",
        (task.id,),
    ) as cur:
        for (raw,) in await cur.fetchall():
            texts += _strings(json.loads(raw))
    artifacts = task.continuation.artifacts if task.continuation else []
    return extract(texts, artifacts, repos.get(task.project), set(repos.values()))


@dataclass(frozen=True)
class PrObservation:
    """Forge facts for one PR, including base/head for prerequisite evidence."""

    state: PrState
    merged_at: datetime | None
    checked_at: datetime
    base_ref: str | None
    head_sha: str | None


_Fetch = tuple[PrState, datetime | None, str | None, str | None]


class PrChecker:
    """Reads one PR from its forge. Concurrent reads of the same PR share one request and a
    result is reused for CACHE_S seconds, so a polling dashboard cannot hammer the forge."""

    def __init__(
        self,
        secrets: SecretStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.secrets = secrets
        self.transport = transport
        self._flight: dict[PrRef, asyncio.Task[_Fetch]] = {}
        self._cache: dict[PrRef, tuple[float, _Fetch, datetime]] = {}

    async def check(
        self, conn: aiosqlite.Connection, ref: PrRef
    ) -> tuple[PrState, datetime | None, datetime]:
        obs = await self.inspect(conn, ref)
        return obs.state, obs.merged_at, obs.checked_at

    async def inspect(self, conn: aiosqlite.Connection, ref: PrRef) -> PrObservation:
        """Same cache and coalescing as `check`, plus base_ref and head_sha."""
        hit = self._cache.get(ref)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            state, merged_at, base_ref, head_sha = hit[1]
            return PrObservation(state, merged_at, hit[2], base_ref, head_sha)
        login = await self._token(
            conn, ref.provider
        )  # read before any await that may be shared
        return await self.inspect_resolved(ref, login)

    async def inspect_resolved(
        self, ref: PrRef, login: tuple[str, str] | None
    ) -> PrObservation:
        """Like `inspect` when the account token was already read; no DB access."""
        hit = self._cache.get(ref)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            state, merged_at, base_ref, head_sha = hit[1]
            return PrObservation(state, merged_at, hit[2], base_ref, head_sha)
        flight = self._flight.get(ref)
        if flight is None:
            flight = asyncio.create_task(self._fetch(ref, login))
            self._flight[ref] = flight
            flight.add_done_callback(lambda _: self._flight.pop(ref, None))
        try:
            state, merged_at, base_ref, head_sha = await asyncio.shield(flight)
        except Exception:  # unreachable by design; unknown beats an error page
            state, merged_at, base_ref, head_sha = PrState.UNKNOWN, None, None, None
        checked_at = utcnow()
        self._cache[ref] = (
            time.monotonic(),
            (state, merged_at, base_ref, head_sha),
            checked_at,
        )
        return PrObservation(state, merged_at, checked_at, base_ref, head_sha)

    async def _token(
        self, conn: aiosqlite.Connection, provider: str
    ) -> tuple[str, str] | None:
        async with conn.execute(
            "SELECT secret_ref FROM accounts WHERE provider = ? AND state != 'disabled'"
            " ORDER BY priority DESC, name",
            (provider,),
        ) as cur:
            rows = await cur.fetchall()
        for (secret_ref,) in rows:
            name = ref_name(secret_ref)
            value = self.secrets.get(name) if name else None
            if value and isinstance(value.get("token"), str):
                return str(value.get("username", "")), value["token"]
        return None

    async def _fetch(self, ref: PrRef, login: tuple[str, str] | None) -> _Fetch:
        if login is None:
            return PrState.UNKNOWN, None, None, None
        path = f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
        github = ref.provider == "github"
        url = (
            f"https://api.github.com/{path}"
            if github
            else f"https://{ref.host}/api/v1/{path}"
        )
        headers = {"Authorization": f"Bearer {login[1]}"} if github else None
        basic = None if github else httpx.BasicAuth(*login)
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=TIMEOUT_S, follow_redirects=False
            ) as client:
                res = await client.get(url, headers=headers, auth=basic)
            if res.status_code != 200:
                return PrState.UNKNOWN, None, None, None
            body = res.json()
            base = body.get("base") if isinstance(body.get("base"), dict) else {}
            head = body.get("head") if isinstance(body.get("head"), dict) else {}
            base_ref = base.get("ref") if isinstance(base.get("ref"), str) else None
            head_sha = head.get("sha") if isinstance(head.get("sha"), str) else None
            if body.get("merged") is True:
                raw = body.get("merged_at")
                merged_at = datetime.fromisoformat(raw) if raw else None
                return PrState.MERGED, merged_at, base_ref, head_sha
            state = {"open": PrState.OPEN, "closed": PrState.CLOSED}.get(
                body.get("state"), PrState.UNKNOWN
            )
            return state, None, base_ref, head_sha
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            log.info("pr state unavailable for %s", ref.url)  # no credential in it
            return PrState.UNKNOWN, None, None, None


async def accept_offer(
    conn: aiosqlite.Connection, task: Task
) -> tuple[str | None, AcceptBlocked | None]:
    """(decision id, reason not offered). Accepting is the existing review decision:
    only while the task awaits review, and never over pending checklist items."""
    if task.state is not TaskState.IN_REVIEW:
        return None, "not_in_review"
    if task.continuation and task.continuation.pending:
        return None, "checklist_pending"
    async with conn.execute(
        "SELECT id FROM decisions WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = 'review' AND state = 'open' ORDER BY created_at DESC LIMIT 1",
        (task.id,),
    ) as cur:
        row = await cur.fetchone()
    return (row[0], None) if row else (None, "no_review")


async def delivery_of(
    conn: aiosqlite.Connection, checker: PrChecker, task: Task
) -> Delivery | None:
    """None when the task names no unambiguous PR. Called with a read connection,
    outside any write transaction."""
    ref = await reference(conn, task)
    if ref is None:
        return None
    state, merged_at, checked_at = await checker.check(conn, ref)
    merged = state is PrState.MERGED
    offer, blocked = await accept_offer(conn, task) if merged else (None, None)
    return Delivery(
        pr=ref.url,
        pr_state=state,
        merged_at=merged_at,
        checked_at=checked_at,
        flags=[MERGED_PENDING_REVIEW] if merged else [],
        accept_decision_id=offer,
        accept_blocked=blocked,
    )
