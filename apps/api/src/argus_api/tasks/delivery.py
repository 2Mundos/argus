"""Pull-request delivery projection (docs/specs/task-delivery.spec.md).

Read-only. References are taken only from structured artifacts and unambiguous
text; a bare number needs the repository the project proves. The forge calls
(Gitea, or GitHub for github.com) use the registered account of that provider,
stay outside write transactions, coalesce per PR, and never invent green on
partial failure. Existing Delivery top-level fields are preserved; additive
fields carry exact-head checks, mergeability, review evidence, checklist and
authorization separately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
import httpx2 as httpx
from argus_core.accounts import repository_provider
from argus_core.continuation import ArtifactKind, ContinuationArtifact, ItemStatus
from argus_core.delivery import (
    AUTH_NOTES,
    MERGED_PENDING_REVIEW,
    AcceptBlocked,
    CheckState,
    Delivery,
    DeliveryAuthorization,
    DeliveryCheck,
    DeliveryChecklist,
    DeliveryReviewEvidence,
    DeliveryTask,
    FactOrigin,
    IndependentReviewSummary,
    MergeFact,
    PrState,
    RequiredGates,
    ShaFact,
)
from argus_core.tasks import Task, TaskState

from argus_api.db import utcnow
from argus_api.fleet import service as fleet
from argus_api.secrets import SecretStore, ref_name

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0
OVERALL_BUDGET_S = 8.0
CACHE_S = 15.0
TEXT_CAP = 20_000
CHECK_CAP = 50
REVIEW_CAP = 20
DESC_CAP = 200
PENDING_IDS_CAP = 20

_URL = re.compile(
    r"https://[^\s/<>()\[\]\"']+/[^\s/<>()\[\]\"']+/[^\s/<>()\[\]\"']+/pulls?/(\d{1,9})(?![\w/])"
)
_BARE = re.compile(r"\b(?:PR|pull request) #(\d{1,9})\b", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)

_FAILED_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "stale",
    }
)


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


@dataclass
class ForgeSnapshot:
    """Everything one forge read produced for a PR (cached)."""

    pr_state: PrState
    merged_at: datetime | None
    head_sha: str | None = None
    head_ref: str | None = None
    base_sha: str | None = None
    base_ref: str | None = None
    mergeable: bool | None = None
    mergeable_state: str | None = None
    checks: list[DeliveryCheck] = field(default_factory=list)
    checks_combined: CheckState = CheckState.UNKNOWN
    required_contexts: list[str] | None = None  # None = unknown
    forge_review_count: int | None = None
    forge_reviews_for_head: int | None = None
    partial: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=utcnow)


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


def task_reported_head(task: Task) -> str | None:
    """Best Argus-known head SHA for staleness comparison; never from forge."""
    if task.continuation:
        for a in task.continuation.artifacts:
            if a.kind is ArtifactKind.PR and a.head_sha and _SHA.fullmatch(a.head_sha):
                return a.head_sha.lower()
        for a in task.continuation.artifacts:
            if a.kind is ArtifactKind.COMMIT:
                sha = a.head_sha or (a.ref if _SHA.fullmatch(a.ref) else None)
                if sha and _SHA.fullmatch(sha):
                    return sha.lower()
    data = (task.result.data if task.result else None) or {}
    for key in ("head_sha", "sha", "commit"):
        val = data.get(key)
        if isinstance(val, str) and _SHA.fullmatch(val):
            return val.lower()
    return None


def allow_merge_from_input(task: Task) -> bool | None:
    """True only when input.delivery.allow_merge is boolean true; False when
    delivery object present without true; None when no delivery input."""
    raw = task.input
    if not isinstance(raw, dict):
        return None
    delivery = raw.get("delivery")
    if not isinstance(delivery, dict):
        return None
    return delivery.get("allow_merge") is True


def _normalize_status(state: str | None) -> CheckState:
    if not state:
        return CheckState.UNKNOWN
    s = state.lower()
    if s in ("success", "successful", "ok", "pass", "passed"):
        return CheckState.SUCCESS
    if s in ("pending", "running", "queued", "in_progress", "expected", "waiting"):
        return CheckState.PENDING
    if s in ("failure", "failed"):
        return CheckState.FAILURE
    if s in ("error", "errored"):
        return CheckState.ERROR
    if s in ("skipped", "neutral", "ignore"):
        return CheckState.SKIPPED
    if s in (
        "cancelled",
        "canceled",
        "timed_out",
        "action_required",
        "startup_failure",
    ):
        return CheckState.CANCELLED
    return CheckState.UNKNOWN


def _check_run_state(run: dict[str, Any]) -> CheckState:
    status = (run.get("status") or "").lower()
    if status and status != "completed":
        return CheckState.PENDING
    conclusion = (run.get("conclusion") or "").lower()
    if conclusion in _FAILED_CONCLUSIONS:
        return CheckState.FAILURE if conclusion == "failure" else CheckState.CANCELLED
    if conclusion == "skipped" or conclusion == "neutral":
        return CheckState.SKIPPED
    if conclusion == "success":
        return CheckState.SUCCESS
    if not conclusion:
        return CheckState.UNKNOWN
    return CheckState.UNKNOWN


def combine_checks(checks: list[DeliveryCheck]) -> CheckState:
    if not checks:
        return CheckState.UNKNOWN
    states = [c.state for c in checks]
    if any(
        s in (CheckState.FAILURE, CheckState.ERROR, CheckState.CANCELLED)
        for s in states
    ):
        return CheckState.FAILURE
    if any(s is CheckState.PENDING for s in states):
        return CheckState.PENDING
    if any(s is CheckState.UNKNOWN for s in states):
        return CheckState.UNKNOWN
    # success and skipped only
    return CheckState.SUCCESS


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    return t if len(t) <= DESC_CAP else t[: DESC_CAP - 1] + "…"


class PrChecker:
    """Reads one PR and its exact-head checks from its forge. Concurrent reads of
    the same PR share one request and a result is reused for CACHE_S seconds."""

    def __init__(
        self,
        secrets: SecretStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.secrets = secrets
        self.transport = transport
        self._flight: dict[PrRef, asyncio.Task[ForgeSnapshot]] = {}
        self._cache: dict[PrRef, tuple[float, ForgeSnapshot]] = {}

    async def check(
        self, conn: aiosqlite.Connection, ref: PrRef
    ) -> tuple[PrState, datetime | None, datetime]:
        """Back-compat: pr_state, merged_at, checked_at."""
        snap = await self.snapshot(conn, ref)
        return snap.pr_state, snap.merged_at, snap.checked_at

    async def snapshot(self, conn: aiosqlite.Connection, ref: PrRef) -> ForgeSnapshot:
        hit = self._cache.get(ref)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        login = await self._token(conn, ref.provider)
        flight = self._flight.get(ref)
        if flight is None:
            flight = asyncio.create_task(self._fetch_all(ref, login))
            self._flight[ref] = flight
            flight.add_done_callback(lambda _: self._flight.pop(ref, None))
        try:
            snap = await asyncio.shield(flight)
        except Exception:
            snap = ForgeSnapshot(pr_state=PrState.UNKNOWN, merged_at=None)
        self._cache[ref] = (time.monotonic(), snap)
        return snap

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

    def _client(
        self, ref: PrRef, login: tuple[str, str]
    ) -> tuple[str, dict[str, str] | None, httpx.BasicAuth | None]:
        github = ref.provider == "github"
        base = (
            f"https://api.github.com/repos/{ref.owner}/{ref.repo}"
            if github
            else f"https://{ref.host}/api/v1/repos/{ref.owner}/{ref.repo}"
        )
        headers = {"Authorization": f"Bearer {login[1]}"} if github else None
        basic = None if github else httpx.BasicAuth(*login)
        return base, headers, basic

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str] | None,
        auth: httpx.BasicAuth | None,
        deadline: float,
    ) -> httpx.Response | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return await client.get(
                url,
                headers=headers,
                auth=auth,
                timeout=min(TIMEOUT_S, remaining),
            )
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def _fetch_all(
        self, ref: PrRef, login: tuple[str, str] | None
    ) -> ForgeSnapshot:
        checked_at = utcnow()
        if login is None:
            return ForgeSnapshot(
                pr_state=PrState.UNKNOWN, merged_at=None, checked_at=checked_at
            )
        base, headers, basic = self._client(ref, login)
        deadline = time.monotonic() + OVERALL_BUDGET_S
        partial: list[str] = []
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=TIMEOUT_S, follow_redirects=False
            ) as client:
                res = await self._get(
                    client, f"{base}/pulls/{ref.number}", headers, basic, deadline
                )
                if res is None or res.status_code != 200:
                    return ForgeSnapshot(
                        pr_state=PrState.UNKNOWN,
                        merged_at=None,
                        partial=["pr"],
                        checked_at=checked_at,
                    )
                body = res.json()
                snap = self._parse_pr(body, checked_at)
                head = snap.head_sha
                if not head:
                    partial.append("head")
                    snap.partial = partial
                    return snap
                await self._fill_statuses(
                    client, base, headers, basic, ref, head, snap, partial, deadline
                )
                if ref.provider == "github":
                    await self._fill_check_runs(
                        client, base, headers, basic, head, snap, partial, deadline
                    )
                await self._fill_required(
                    client, base, headers, basic, ref, snap, partial, deadline
                )
                await self._fill_reviews(
                    client, base, headers, basic, ref, head, snap, partial, deadline
                )
                snap.partial = partial
                if not snap.checks and (
                    "statuses" in partial or "check_runs" in partial
                ):
                    snap.checks_combined = CheckState.UNKNOWN
                else:
                    snap.checks_combined = combine_checks(snap.checks)
                # Mark required on checks when gates are known.
                if snap.required_contexts is not None:
                    req = set(snap.required_contexts)
                    for c in snap.checks:
                        c.required = c.name in req
                return snap
        except (
            httpx.HTTPError,
            ValueError,
            AttributeError,
            TypeError,
            json.JSONDecodeError,
        ):
            log.info("pr state unavailable for %s", ref.url)
            return ForgeSnapshot(
                pr_state=PrState.UNKNOWN, merged_at=None, checked_at=checked_at
            )

    def _parse_pr(self, body: dict[str, Any], checked_at: datetime) -> ForgeSnapshot:
        merged_at = None
        if body.get("merged") is True:
            state = PrState.MERGED
            raw = body.get("merged_at")
            if raw:
                try:
                    merged_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    merged_at = None
        else:
            state = {"open": PrState.OPEN, "closed": PrState.CLOSED}.get(
                body.get("state"), PrState.UNKNOWN
            )
        head_obj = body.get("head")
        base_obj = body.get("base")
        head: dict[str, Any] = head_obj if isinstance(head_obj, dict) else {}
        base: dict[str, Any] = base_obj if isinstance(base_obj, dict) else {}
        head_sha = head["sha"] if isinstance(head.get("sha"), str) else None
        base_sha = base["sha"] if isinstance(base.get("sha"), str) else None
        mergeable = body.get("mergeable")
        if not isinstance(mergeable, bool):
            mergeable = None
        mergeable_state = body.get("mergeable_state")
        if not isinstance(mergeable_state, str):
            mergeable_state = None
        head_ref = head["ref"] if isinstance(head.get("ref"), str) else None
        base_ref = base["ref"] if isinstance(base.get("ref"), str) else None
        return ForgeSnapshot(
            pr_state=state,
            merged_at=merged_at,
            head_sha=head_sha.lower() if head_sha else None,
            head_ref=head_ref,
            base_sha=base_sha.lower() if base_sha else None,
            base_ref=base_ref,
            mergeable=mergeable,
            mergeable_state=mergeable_state,
            checked_at=checked_at,
        )

    async def _fill_statuses(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict[str, str] | None,
        auth: httpx.BasicAuth | None,
        ref: PrRef,
        head: str,
        snap: ForgeSnapshot,
        partial: list[str],
        deadline: float,
    ) -> None:
        github = ref.provider == "github"
        url = (
            f"{base}/commits/{head}/status?per_page=100"
            if github
            else f"{base}/commits/{head}/statuses?limit=50&page=1"
        )
        res = await self._get(client, url, headers, auth, deadline)
        if res is None or res.status_code != 200:
            partial.append("statuses")
            return
        try:
            data = res.json()
        except (ValueError, TypeError):
            partial.append("statuses")
            return
        raw_items: Any
        if github:
            raw_items = data.get("statuses") if isinstance(data, dict) else None
        elif isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict):
            raw_items = data.get("statuses")
        else:
            raw_items = None
        if not isinstance(raw_items, list):
            partial.append("statuses")
            return
        # Latest per context.
        latest: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            ctx = item.get("context") or item.get("name")
            if not isinstance(ctx, str) or not ctx:
                continue
            latest[ctx] = item
        if len(latest) > CHECK_CAP:
            partial.append("truncated_checks")
        for i, (name, item) in enumerate(latest.items()):
            if i >= CHECK_CAP:
                break
            snap.checks.append(
                DeliveryCheck(
                    name=name,
                    state=_normalize_status(
                        item.get("state")
                        if isinstance(item.get("state"), str)
                        else None
                    ),
                    kind="status",
                    sha=head,
                    description=_truncate(
                        item.get("description")
                        if isinstance(item.get("description"), str)
                        else None
                    ),
                    target_url=_truncate(
                        item.get("target_url")
                        if isinstance(item.get("target_url"), str)
                        else None
                    ),
                    checked_at=snap.checked_at,
                )
            )

    async def _fill_check_runs(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict[str, str] | None,
        auth: httpx.BasicAuth | None,
        head: str,
        snap: ForgeSnapshot,
        partial: list[str],
        deadline: float,
    ) -> None:
        res = await self._get(
            client,
            f"{base}/commits/{head}/check-runs?per_page=50",
            headers,
            auth,
            deadline,
        )
        if res is None or res.status_code != 200:
            partial.append("check_runs")
            return
        try:
            data = res.json()
        except (ValueError, TypeError):
            partial.append("check_runs")
            return
        runs = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            partial.append("check_runs")
            return
        # Latest per name.
        latest: dict[str, dict[str, Any]] = {}
        for run in runs:
            if not isinstance(run, dict):
                continue
            name = run.get("name")
            if not isinstance(name, str) or not name:
                continue
            latest[name] = run
        room = CHECK_CAP - len(snap.checks)
        if len(latest) > room:
            partial.append("truncated_checks")
        for i, (name, run) in enumerate(latest.items()):
            if i >= max(room, 0):
                break
            out_raw = run.get("output")
            output: dict[str, Any] = out_raw if isinstance(out_raw, dict) else {}
            title = output.get("title")
            html_url = run.get("html_url")
            snap.checks.append(
                DeliveryCheck(
                    name=name,
                    state=_check_run_state(run),
                    kind="check_run",
                    sha=head,
                    description=_truncate(title if isinstance(title, str) else None),
                    target_url=_truncate(
                        html_url if isinstance(html_url, str) else None
                    ),
                    checked_at=snap.checked_at,
                )
            )

    async def _fill_required(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict[str, str] | None,
        auth: httpx.BasicAuth | None,
        ref: PrRef,
        snap: ForgeSnapshot,
        partial: list[str],
        deadline: float,
    ) -> None:
        branch = snap.base_ref
        if not branch:
            # Unknown gates — do not invent empty.
            return
        if ref.provider == "github":
            res = await self._get(
                client,
                f"{base}/branches/{branch}/protection/required_status_checks",
                headers,
                auth,
                deadline,
            )
            if res is None:
                partial.append("required_gates")
                return
            if res.status_code in (401, 403, 404):
                # Not disclosed to this credential: unknown, not empty.
                return
            if res.status_code != 200:
                partial.append("required_gates")
                return
            try:
                data = res.json()
            except (ValueError, TypeError):
                partial.append("required_gates")
                return
            contexts: list[str] = []
            raw = data.get("contexts") if isinstance(data, dict) else None
            if isinstance(raw, list):
                contexts.extend(c for c in raw if isinstance(c, str))
            checks = data.get("checks") if isinstance(data, dict) else None
            if isinstance(checks, list):
                contexts.extend(
                    c["context"]
                    for c in checks
                    if isinstance(c, dict) and isinstance(c.get("context"), str)
                )
            # Dedupe, preserve order.
            seen: set[str] = set()
            ordered = []
            for c in contexts:
                if c not in seen:
                    seen.add(c)
                    ordered.append(c)
            snap.required_contexts = ordered
            return
        # Gitea: branch protection may list status_check_contexts.
        res = await self._get(
            client,
            f"{base}/branch_protections/{branch}",
            headers,
            auth,
            deadline,
        )
        if res is None:
            partial.append("required_gates")
            return
        if res.status_code in (401, 403, 404):
            return
        if res.status_code != 200:
            # Some Gitea versions use a list endpoint.
            res2 = await self._get(
                client, f"{base}/branch_protections", headers, auth, deadline
            )
            if res2 is None or res2.status_code != 200:
                partial.append("required_gates")
                return
            try:
                items = res2.json()
            except (ValueError, TypeError):
                partial.append("required_gates")
                return
            if not isinstance(items, list):
                partial.append("required_gates")
                return
            match = next(
                (
                    p
                    for p in items
                    if isinstance(p, dict)
                    and (p.get("branch_name") == branch or p.get("rule_name") == branch)
                ),
                None,
            )
            if match is None:
                return
            data = match
        else:
            try:
                data = res.json()
            except (ValueError, TypeError):
                partial.append("required_gates")
                return
        raw = data.get("status_check_contexts") if isinstance(data, dict) else None
        if isinstance(raw, list):
            snap.required_contexts = [c for c in raw if isinstance(c, str)]
        # else leave unknown

    async def _fill_reviews(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict[str, str] | None,
        auth: httpx.BasicAuth | None,
        ref: PrRef,
        head: str,
        snap: ForgeSnapshot,
        partial: list[str],
        deadline: float,
    ) -> None:
        url = (
            f"{base}/pulls/{ref.number}/reviews?per_page={REVIEW_CAP}"
            if ref.provider == "github"
            else f"{base}/pulls/{ref.number}/reviews?limit={REVIEW_CAP}&page=1"
        )
        res = await self._get(client, url, headers, auth, deadline)
        if res is None or res.status_code != 200:
            partial.append("reviews")
            return
        try:
            data = res.json()
        except (ValueError, TypeError):
            partial.append("reviews")
            return
        items = (
            data
            if isinstance(data, list)
            else data.get("items")
            if isinstance(data, dict)
            else None
        )
        if not isinstance(items, list):
            partial.append("reviews")
            return
        if len(items) >= REVIEW_CAP:
            partial.append("truncated_reviews")
        total = 0
        for_head = 0
        for item in items[:REVIEW_CAP]:
            if not isinstance(item, dict):
                continue
            total += 1
            commit = item.get("commit_id") or item.get("commitId")
            if isinstance(commit, str) and commit.lower().startswith(head[:7]):
                for_head += 1
        snap.forge_review_count = total
        snap.forge_reviews_for_head = for_head


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


async def _independent_review(
    conn: aiosqlite.Connection, task: Task
) -> IndependentReviewSummary | None:
    """Summary of a code_review child, if any. Source is always task (Argus)."""
    async with conn.execute(
        "SELECT id, state, result FROM tasks WHERE parent_id = ? AND kind = 'code_review'"
        " ORDER BY created_at DESC LIMIT 1",
        (task.id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    child_id, state, raw = row[0], row[1], row[2]
    verdict = "pending"
    head_sha = None
    if state in ("succeeded", "failed", "cancelled"):
        verdict = "unknown"
    if raw:
        try:
            result = json.loads(raw)
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, dict):
                v = data.get("verdict")
                if isinstance(v, str):
                    verdict = v
                hs = data.get("head_sha")
                if isinstance(hs, str):
                    head_sha = hs.lower()
        except (TypeError, json.JSONDecodeError):
            pass
    if state in (
        "ready",
        "claimed",
        "pending",
        "blocked",
        "in_review",
        "awaiting_approval",
    ):
        verdict = "pending"
    return IndependentReviewSummary(
        child_task_id=child_id, verdict=verdict, head_sha=head_sha
    )


def _checklist(task: Task) -> DeliveryChecklist | None:
    cont = task.continuation
    if cont is None or not cont.checklist:
        if cont is None:
            return DeliveryChecklist(pending=0, done=0, total=0, pending_ids=[])
        return DeliveryChecklist(pending=0, done=0, total=0, pending_ids=[])
    pending_ids = [i.id for i in cont.checklist if i.status is ItemStatus.PENDING][
        :PENDING_IDS_CAP
    ]
    done = sum(1 for i in cont.checklist if i.status is ItemStatus.DONE)
    pending = sum(1 for i in cont.checklist if i.status is ItemStatus.PENDING)
    return DeliveryChecklist(
        pending=pending,
        done=done,
        total=len(cont.checklist),
        pending_ids=pending_ids,
    )


async def delivery_of(
    conn: aiosqlite.Connection, checker: PrChecker, task: Task
) -> Delivery | None:
    """None when the task names no unambiguous PR. Called with a read connection,
    outside any write transaction."""
    ref = await reference(conn, task)
    if ref is None:
        return None
    snap = await checker.snapshot(conn, ref)
    merged = snap.pr_state is PrState.MERGED
    # Top-level accept_* stay merged-only (existing clients); authorization mirrors them.
    offer, blocked = await accept_offer(conn, task) if merged else (None, None)

    reported = task_reported_head(task)
    forge_head = snap.head_sha
    stale = bool(
        reported
        and forge_head
        and not (
            reported == forge_head
            or forge_head.startswith(reported)
            or reported.startswith(forge_head)
        )
    )
    if forge_head and any(c.sha != forge_head for c in snap.checks):
        stale = True

    checked_at = snap.checked_at
    head = ShaFact(
        sha=forge_head or reported,
        ref=snap.head_ref,
        source=(
            FactOrigin.FORGE
            if forge_head
            else (FactOrigin.TASK if reported else FactOrigin.UNKNOWN)
        ),
        checked_at=checked_at if forge_head else None,
    )
    base = ShaFact(
        sha=snap.base_sha,
        ref=snap.base_ref,
        source=(
            FactOrigin.FORGE if snap.base_sha or snap.base_ref else FactOrigin.UNKNOWN
        ),
        checked_at=checked_at if snap.base_sha or snap.base_ref else None,
    )
    merge = MergeFact(
        mergeable=snap.mergeable,
        mergeable_state=snap.mergeable_state,
        source=(
            FactOrigin.FORGE
            if snap.mergeable is not None or snap.mergeable_state
            else FactOrigin.UNKNOWN
        ),
        checked_at=checked_at
        if snap.mergeable is not None or snap.mergeable_state
        else None,
    )
    required = (
        RequiredGates(contexts=snap.required_contexts, checked_at=checked_at)
        if snap.required_contexts is not None
        else None
    )
    independent = await _independent_review(conn, task)
    review = DeliveryReviewEvidence(
        independent=independent,
        forge_review_count=snap.forge_review_count,
        forge_reviews_for_head=snap.forge_reviews_for_head,
        agentic_finding_count=None,  # unknown; never invent zero
        sha=forge_head,
        source=(
            FactOrigin.FORGE
            if snap.forge_review_count is not None
            else (FactOrigin.TASK if independent else FactOrigin.UNKNOWN)
        ),
        checked_at=checked_at if snap.forge_review_count is not None else None,
    )
    disclosures = [
        "Read-only forge observation; Argus does not merge or accept from this read.",
        AUTH_NOTES,
    ]
    if snap.partial:
        disclosures.append("Partial forge read: " + ", ".join(snap.partial) + ".")
    if stale:
        disclosures.append(
            "Stale: task-reported head disagrees with forge head, or checks are off-head."
        )

    return Delivery(
        pr=ref.url,
        pr_state=snap.pr_state,
        merged_at=snap.merged_at,
        checked_at=checked_at,
        flags=[MERGED_PENDING_REVIEW] if merged else [],
        accept_decision_id=offer,
        accept_blocked=blocked,
        task=DeliveryTask(id=task.id, state=task.state.value, project=task.project),
        head=head,
        base=base,
        merge=merge,
        checks=snap.checks,
        checks_combined=snap.checks_combined,
        required_gates=required,
        review=review,
        checklist=_checklist(task),
        authorization=DeliveryAuthorization(
            allow_merge=allow_merge_from_input(task),
            accept_decision_id=offer,
            accept_blocked=blocked,
        ),
        stale=stale,
        partial=list(snap.partial),
        disclosures=disclosures,
    )
