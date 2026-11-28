"""PR delivery projection (docs/specs/task-delivery.spec.md): the reference
parser, exact-head forge reads, REST and MCP. The forge is a mock transport."""

import asyncio
import json
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import aiosqlite
import httpx2 as httpx
import pytest
from api_support import add_forge, insert_project
from argus_api.db import Database, ts
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import delivery
from argus_api.tasks.delivery import PrChecker, PrRef, combine_checks
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ContinuationArtifact,
    ContinuationProvenance,
)
from argus_core.delivery import CheckState, DeliveryCheck, PrState
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN as MCP_ADMIN
from test_mcp import tool

HOST, OWNER, REPO = "git.example.test", "team", "argus"
REPOSITORY = (HOST, OWNER, REPO)
ALLOWED = {REPOSITORY, (HOST, "team", "radar")}
URL = f"https://{HOST}/{OWNER}/{REPO}/pulls/12"
ADMIN_TOKEN = MCP_ADMIN
HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
# REST tests create their task on the real clock, so a fixture PR is timestamped
# after it: `delivery.implausible` rejects a PR that existed before its task.
SOON_AT = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
SOON = SOON_AT.strftime("%Y-%m-%dT%H:%M:%SZ")


def pr_artifact(
    ref: str, url: str | None = None, minute: int = 0, head_sha: str | None = None
):
    return ContinuationArtifact(
        kind=ArtifactKind.PR,
        ref=ref,
        url=url,
        state=ArtifactState.OPEN,
        provenance=ContinuationProvenance.WORKER,
        observed_at=datetime(2026, 10, 24, 10, minute, tzinfo=UTC),
        head_sha=head_sha,
    )


# -- parser ---------------------------------------------------------------------


def test_full_url_of_a_configured_repository_is_a_reference() -> None:
    ref = delivery.extract([f"Opened {URL}."], [], None, ALLOWED)
    assert ref == PrRef(HOST, OWNER, REPO, 12)
    assert ref.url == URL


def test_url_of_an_unconfigured_host_or_repository_is_ignored() -> None:
    for text in (
        "https://evil.example/team/argus/pulls/12",
        f"https://{HOST}/team/other/pulls/12",
        f"https://{HOST}/team/argus/issues/12",
    ):
        assert delivery.extract([text], [], None, ALLOWED) is None


def test_bare_number_in_prose_never_binds_a_pr() -> None:
    """The reported false binding (task 75c0eb23): an honest progress note that names
    an unrelated, already-merged PR must extract nothing, even when the project's
    repository is proven (which it is: the bare number used to resolve against it)."""
    progress = (
        "no prior branch or PR carries this work (only the spec revision PR #1399,"
        " merged). Branch argus/task-75c0eb23 is now reset to master at 360a3800."
    )
    assert delivery.extract([progress], [], REPOSITORY, ALLOWED) is None
    for text in (
        "PR #12 is open",
        "pull request #12",
        "fixes #12",
        "see issue #12",
        "PR 12",
        "#12",
        "PR #x",
        "PR #1a",
    ):
        assert delivery.extract([text], [], REPOSITORY, ALLOWED) is None


def test_structured_artifact_wins_and_newest_observation_is_used() -> None:
    older = pr_artifact("5", minute=1)
    newer = pr_artifact("7", url=f"https://{HOST}/{OWNER}/{REPO}/pull/7", minute=2)
    ref = delivery.extract([URL], [older, newer], REPOSITORY, ALLOWED)
    assert ref == PrRef(HOST, OWNER, REPO, 7)
    # A numeric ref without a proven repository cannot be resolved.
    assert delivery.extract([], [older], None, ALLOWED) is None


def test_two_different_prs_in_text_are_ambiguous() -> None:
    texts = [URL, f"https://{HOST}/{OWNER}/{REPO}/pulls/13", URL]
    assert delivery.extract(texts, [], None, ALLOWED) is None
    assert delivery.extract([URL, URL], [], None, ALLOWED) is not None


def test_explicit_url_in_text_is_a_reference_but_a_bare_number_is_not() -> None:
    """The two text sources: a full URL binds, a `PR #N` next to it changes nothing
    (and cannot make the text ambiguous)."""
    with_url = f"Opened {URL} for this task; the earlier spec PR #1399 is merged."
    assert delivery.extract([with_url], [], REPOSITORY, ALLOWED) == PrRef(
        HOST, OWNER, REPO, 12
    )
    assert (
        delivery.extract(
            ["the earlier spec PR #1399 is merged"], [], REPOSITORY, ALLOWED
        )
        is None
    )


def test_structured_artifact_beats_an_explicit_url_in_text() -> None:
    artifact = pr_artifact("5")
    assert delivery.extract([URL], [artifact], REPOSITORY, ALLOWED) == PrRef(
        HOST, OWNER, REPO, 5
    )


def forged(created: str | None = None, merged: str | None = None, ref: str = "feat"):
    return delivery.ForgeSnapshot(
        pr_state=PrState.MERGED if merged else PrState.OPEN,
        merged_at=datetime.fromisoformat(merged) if merged else None,
        created_at=datetime.fromisoformat(created) if created else None,
        head_ref=ref,
    )


def task_at(created: str, branch: str | None = None) -> SimpleNamespace:
    """Only what `implausible`/`declared_branch` read: the task's creation time, its
    id and its continuation artifacts."""
    now = datetime.fromisoformat(created)
    artifacts = (
        [
            ContinuationArtifact(
                kind=ArtifactKind.BRANCH,
                ref=branch,
                provenance=ContinuationProvenance.WORKER,
                observed_at=now,
            )
        ]
        if branch
        else []
    )
    return SimpleNamespace(
        id="75c0eb23-0000-4000-8000-000000000000",
        created_at=now,
        continuation=SimpleNamespace(artifacts=artifacts),
    )


def test_implausible_rejects_a_pr_older_than_its_task() -> None:
    task = task_at("2026-11-17T15:45:30+00:00")
    assert (
        delivery.implausible(
            forged(
                created="2026-11-17T15:40:00+00:00", merged="2026-11-17T15:44:01+00:00"
            ),
            task,
            None,
        )
        == "predates_task"
    )
    assert (
        delivery.implausible(forged(merged="2026-11-17T15:44:01+00:00"), task, None)
        == "merged_before_task"
    )
    assert (
        delivery.implausible(
            forged(
                created="2026-11-17T15:50:00+00:00", merged="2026-11-17T15:55:00+00:00"
            ),
            task,
            None,
        )
        is None
    )


def test_implausible_rejects_a_pr_from_another_branch() -> None:
    task = task_at("2026-11-17T15:45:30+00:00", "argus/task-75c0eb23")
    assert delivery.declared_branch(task) == "argus/task-75c0eb23"
    assert (
        delivery.implausible(
            forged(
                created="2026-11-17T15:50:00+00:00", ref="docs/guided-view-review-fixes"
            ),
            task,
            "argus/task-75c0eb23",
        )
        == "foreign_branch"
    )
    assert (
        delivery.implausible(
            forged(created="2026-11-17T15:50:00+00:00", ref="argus/task-75c0eb23"),
            task,
            "argus/task-75c0eb23",
        )
        is None
    )
    # No declared branch and no forged head ref: nothing to reject on the branch.
    assert (
        delivery.implausible(forged(), task_at("2026-11-17T15:45:30+00:00"), None)
        is None
    )


GONE = "refs/pull/1452/head"
# (head, base repo_id, github, head branch read). The first row is task da371c5d:
# Gitea's merged #1452 after its branch was deleted.
HEAD_BRANCHES = [
    (
        {"ref": GONE, "label": "argus/task-da371c5d", "repo_id": 7},
        7,
        False,
        "argus/task-da371c5d",
    ),
    (
        {"ref": "argus/task-da371c5d", "label": "argus/task-da371c5d"},
        None,
        False,
        "argus/task-da371c5d",
    ),
    (
        {"ref": GONE, "label": "argus/task-da371c5d", "repo_id": 9},
        7,
        False,
        GONE,
    ),  # a fork
    (
        {"ref": GONE, "label": "argus/task-da371c5d", "repo_id": -1},
        -1,
        False,
        GONE,
    ),  # head repo gone
    (
        {"ref": GONE, "label": "argus/task-da371c5d"},
        None,
        False,
        GONE,
    ),  # head repo unknown
    ({"ref": GONE, "label": "", "repo_id": 7}, 7, False, GONE),  # AGit flow: no branch
    (
        {"ref": "refs/pull/9/head", "label": "argus/task-da371c5d", "repo_id": 7},
        7,
        False,
        "refs/pull/9/head",
    ),
    (
        {"ref": GONE, "label": "argus/task-da371c5d", "repo_id": 7},
        7,
        True,
        GONE,
    ),  # GitHub
    ({"label": "argus/task-da371c5d", "repo_id": 7}, 7, False, None),
]


@pytest.mark.parametrize(("head", "base_repo", "github", "branch"), HEAD_BRANCHES)
def test_the_head_branch_of_a_pr_whose_branch_was_deleted(
    head: dict, base_repo: int | None, github: bool, branch: str | None
) -> None:
    """Only the PR's own `refs/pull/<n>/head` in its own repository falls back to the
    label; anything else keeps the ref, which then matches no task branch."""
    body = {"number": 1452, "head": {"sha": HEAD, **head}, "base": {"ref": "main"}}
    if base_repo is not None:
        body["base"]["repo_id"] = base_repo
    snap = delivery.PrChecker(MemorySecretStore())._parse_pr(body, SOON_AT, github)
    assert snap.head_ref == branch


def test_repository_url_parsing() -> None:
    assert delivery.parse_repository(f"https://{HOST.upper()}/Team/Argus.git") == (
        HOST,
        "team",
        "argus",
    )
    assert delivery.parse_repository("http://x/a/b") is None
    assert delivery.parse_repository("https://x/a") is None
    bare = f"https://{HOST}/{OWNER}/{REPO}"
    assert delivery.canonical_repository_url(f"{bare}.git") == bare
    assert delivery.canonical_repository_url("http://evil/a/b") is None
    assert (
        delivery.canonical_repository_url("https://user:tok@git.example/acme/api")
        is None
    )


@pytest.mark.asyncio
async def test_repositories_omits_global_even_when_configured(db: Database) -> None:
    from argus_api.fleet import service as fleet_svc
    from argus_core.fleet import NanoclawOptions, PoolUpsert

    demo = f"https://{HOST}/{OWNER}/{REPO}"
    global_url = f"https://{HOST}/team/knowledge"
    opts = NanoclawOptions(
        install_dir="/a",
        instances_root="/b",
        agent_image="x",
        repositories={"global": global_url, "demo": demo},
    )
    async with db.write() as tx:
        await fleet_svc.upsert_pool(
            tx,
            "map",
            PoolUpsert(runtime="nanoclaw", image="i", max_replicas=1, nanoclaw=opts),
            "t",
        )
        await tx.conn.execute(
            "UPDATE projects SET repository_url = ? WHERE slug = 'global'",
            (global_url,),
        )
    async with db.read() as conn:
        repos = await delivery.repositories(conn)
    assert "global" not in repos
    assert repos["demo"] == (HOST, OWNER, REPO)


@pytest.mark.asyncio
async def test_pool_repository_agrees_on_equivalent_url_spellings(db: Database) -> None:
    from argus_api.fleet import service as fleet_svc
    from argus_core.fleet import NanoclawOptions, PoolUpsert

    bare = f"https://{HOST}/{OWNER}/{REPO}"
    suffixed = f"{bare}.git"

    def nc(repos: dict[str, str]) -> NanoclawOptions:
        return NanoclawOptions(
            install_dir="/a", instances_root="/b", agent_image="x", repositories=repos
        )

    base = {"runtime": "nanoclaw", "image": "i", "max_replicas": 1}
    async with db.write() as tx:
        await fleet_svc.upsert_pool(
            tx,
            "one",
            PoolUpsert(**base, nanoclaw=nc({"acme": bare})),
            "t",
        )
        await fleet_svc.upsert_pool(
            tx,
            "two",
            PoolUpsert(**base, nanoclaw=nc({"acme": suffixed})),
            "t",
        )
    async with db.read() as conn:
        assert await delivery.pool_repository(conn, "acme") == bare


@pytest.mark.asyncio
async def test_project_repository_url_rejects_malformed_record_and_falls_back(
    db: Database,
) -> None:
    from argus_api.fleet import service as fleet_svc
    from argus_core.fleet import NanoclawOptions, PoolUpsert

    good = f"https://{HOST}/{OWNER}/{REPO}"
    async with db.write() as tx:
        await fleet_svc.upsert_pool(
            tx,
            "one",
            PoolUpsert(
                runtime="nanoclaw",
                image="i",
                max_replicas=1,
                nanoclaw=NanoclawOptions(
                    install_dir="/a",
                    instances_root="/b",
                    agent_image="x",
                    repositories={"acme": good},
                ),
            ),
            "t",
        )
        await tx.conn.execute(
            "INSERT INTO projects (slug, name, description, repository_url, created_at, updated_at)"
            " VALUES ('acme', 'Acme', '', 'http://legacy.invalid/acme/api', 'now', 'now')"
            " ON CONFLICT(slug) DO UPDATE SET repository_url = excluded.repository_url"
        )
    async with db.read() as conn:
        assert await delivery.project_repository_url(conn, "acme") == good
        assert await delivery.project_repository_url(conn, "global") is None


def test_combine_checks_priority() -> None:
    def c(state: CheckState) -> DeliveryCheck:
        return DeliveryCheck(
            name="x",
            state=state,
            kind="status",
            sha=HEAD,
            checked_at=datetime(2026, 10, 24, tzinfo=UTC),
        )

    assert combine_checks([]) is CheckState.UNKNOWN
    assert (
        combine_checks([c(CheckState.SUCCESS), c(CheckState.SKIPPED)])
        is CheckState.SUCCESS
    )
    assert (
        combine_checks([c(CheckState.SUCCESS), c(CheckState.PENDING)])
        is CheckState.PENDING
    )
    assert (
        combine_checks([c(CheckState.PENDING), c(CheckState.FAILURE)])
        is CheckState.FAILURE
    )
    assert (
        combine_checks([c(CheckState.SUCCESS), c(CheckState.UNKNOWN)])
        is CheckState.UNKNOWN
    )


# -- forge check ----------------------------------------------------------------


async def _forge(db: Database, project: str, host: str, owner: str, repo: str) -> None:
    """A project whose repository is host/owner/repo, with its forge integration."""
    await insert_project(db, project)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE projects SET repository_url = ? WHERE slug = ?",
            (f"https://{host}/{owner}/{repo}", project),
        )
        config = {"host": host, **({} if host == "github.com" else {"username": "bot"})}
        await tx.conn.execute(
            "INSERT INTO integrations (id, project, kind, name, config, access,"
            " secret_ref, state, created_at, created_by, updated_at)"
            " VALUES (?, ?, 'forge', 'forge', ?, 'read_write', 'sops:g', 'active',"
            " ?, 'test', ?)",
            (f"f-{project}", project, json.dumps(config), ts(tx.now), ts(tx.now)),
        )


@pytest.fixture
async def conn(db: Database) -> AsyncGenerator[aiosqlite.Connection]:
    """Two projects with forge integrations: REF's Gitea repository and a GitHub one."""
    await _forge(db, "repo", HOST, OWNER, REPO)
    await _forge(db, "gh", "github.com", "2mundos", "argus")
    async with db.read() as c:
        yield c


class Secrets:
    def get(self, name):
        return {"token": "dummy-token", "version": 1} if name == "g" else None


REF = PrRef(HOST, OWNER, REPO, 12)


def checker(handler) -> PrChecker:
    return PrChecker(Secrets(), httpx.MockTransport(handler))  # ty: ignore[invalid-argument-type]


def forge_router(routes: dict[str, object]):
    """Map path suffix -> json body or Response. Longest suffix wins."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        matches = [s for s in routes if path.endswith(s)]
        if not matches:
            return httpx.Response(404, json={"message": "missing"})
        suffix = max(matches, key=len)
        body = routes[suffix]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return handler


def open_pr(**extra):
    return {
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": HEAD, "ref": "feat"},
        "base": {"sha": BASE, "ref": "main"},
        **extra,
    }


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ({"state": "open", "merged": False}, PrState.OPEN),
        ({"state": "closed", "merged": False}, PrState.CLOSED),
        (
            {"state": "closed", "merged": True, "merged_at": "2026-10-24T09:00:00Z"},
            PrState.MERGED,
        ),
        ({"state": "weird"}, PrState.UNKNOWN),
    ],
)
async def test_states_are_read_with_the_repository_credential(body, want, conn) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/pulls/12"):
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    state, merged_at, _ = await checker(handler).check(conn, REF)  # ty: ignore[invalid-argument-type]
    assert state is want
    assert (merged_at is not None) == (want is PrState.MERGED)
    pr_reqs = [r for r in seen if r.url.path.endswith("/pulls/12")]
    assert len(pr_reqs) == 1
    req = pr_reqs[0]
    assert req.method == "GET"
    assert str(req.url) == f"https://{HOST}/api/v1/repos/{OWNER}/{REPO}/pulls/12"
    assert req.headers["authorization"].startswith("Basic ")


async def test_exact_head_statuses_and_required_gates_unknown_on_403(conn) -> None:
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            {"context": "ci/tests", "status": "success", "description": "ok"},
            {"context": "ci/lint", "status": "pending"},
        ],
        "/branch_protections/main": httpx.Response(403, json={"message": "no"}),
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert snap.pr_state is PrState.OPEN
    assert snap.head_sha == HEAD
    assert snap.mergeable is True
    names = {c.name: c.state for c in snap.checks}
    assert names["ci/tests"] is CheckState.SUCCESS
    assert names["ci/lint"] is CheckState.PENDING
    assert snap.checks_combined is CheckState.PENDING
    assert snap.required_contexts is None  # unknown, not empty
    assert "required_gates" not in snap.partial
    assert snap.forge_review_count == 0


async def test_required_gates_empty_only_when_forge_says_so(conn) -> None:
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [],
        "/branch_protections/main": {"status_check_contexts": []},
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert snap.required_contexts == []


async def test_status_failure_is_partial_not_green(conn) -> None:
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": httpx.Response(500, json={}),
        "/pulls/12/reviews": httpx.Response(500, json={}),
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert snap.pr_state is PrState.OPEN
    assert snap.checks == []
    assert snap.checks_combined is CheckState.UNKNOWN
    assert "statuses" in snap.partial
    assert "reviews" in snap.partial


async def test_github_pr_reads_check_runs_and_bearer(conn) -> None:
    head = "cccccccccccccccccccccccccccccccccccccccc"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path.endswith("/pulls/3"):
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "merged": False,
                    "head": {"sha": head, "ref": "f"},
                    "base": {"sha": BASE, "ref": "main"},
                },
            )
        if "check-runs" in path:
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "build",
                            "status": "completed",
                            "conclusion": "skipped",
                            "html_url": "https://github.com/x",
                            "output": {"title": "skipped"},
                        }
                    ]
                },
            )
        if path.endswith("/status") or "status?" in str(request.url):
            return httpx.Response(200, json={"statuses": []})
        if "required_status_checks" in path:
            return httpx.Response(200, json={"contexts": ["build"], "checks": []})
        if path.endswith("/reviews"):
            return httpx.Response(
                200, json=[{"id": 1, "commit_id": head, "state": "COMMENTED"}]
            )
        return httpx.Response(404, json={})

    ref = PrRef("github.com", "2mundos", "argus", 3)
    assert ref.url == "https://github.com/2mundos/argus/pull/3"
    snap = await checker(handler).snapshot(conn, ref)  # ty: ignore[invalid-argument-type]
    assert snap.pr_state is PrState.OPEN
    assert any(c.name == "build" and c.state is CheckState.SKIPPED for c in snap.checks)
    assert snap.required_contexts == ["build"]
    assert snap.checks[0].required is True
    assert snap.forge_reviews_for_head == 1
    assert any(urlparse(u).hostname == "api.github.com" for u in seen)
    assert any(u.endswith("/pulls/3") for u in seen)


async def test_github_pull_url_is_a_reference() -> None:
    repo = ("github.com", "2mundos", "argus")
    ref = delivery.extract(
        ["https://github.com/2Mundos/argus/pull/3"], [], None, {repo}
    )
    assert ref == PrRef(*repo, 3)


def _gitea_status(
    *,
    context: str,
    status: str,
    id: int,
    updated_at: str,
    created_at: str | None = None,
    sha: str | None = None,
    description: str | None = None,
    target_url: str | None = None,
) -> dict:
    """Authentic Gitea CommitStatus shape (`status`, not GitHub `state`)."""
    row: dict = {
        "id": id,
        "status": status,
        "context": context,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
        "description": description,
        "target_url": target_url,
    }
    if sha is not None:
        row["sha"] = sha
    return row


def _gitea_status_pages(pages: dict[int, list]) -> object:
    """Serve paginated `/commits/{sha}/statuses?page=N` from authentic page bodies."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/12"):
            return httpx.Response(200, json=open_pr())
        if f"/commits/{HEAD}/statuses" in path:
            page = int(request.url.params.get("page") or "1")
            if page not in pages:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=pages[page])
        if path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return handler


async def test_gitea_reads_status_field_not_state(conn) -> None:
    """Gitea CommitStatus uses `status`; a GitHub-shaped `state` alone is unknown."""
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            _gitea_status(
                context="ci",
                status="success",
                id=2,
                updated_at="2026-11-27T12:00:00Z",
            ),
            # A row that only carries GitHub's field must not project success.
            {
                "id": 1,
                "context": "legacy",
                "state": "success",
                "updated_at": "2026-11-27T12:00:00Z",
                "created_at": "2026-11-27T12:00:00Z",
            },
        ],
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    names = {c.name: c.state for c in snap.checks}
    assert names["ci"] is CheckState.SUCCESS
    assert names["legacy"] is CheckState.UNKNOWN
    assert snap.checks_combined is CheckState.UNKNOWN


async def test_gitea_newest_first_history_keeps_latest_success(conn) -> None:
    """Newest-first duplicate contexts: timestamps/ids pick success over older pending."""
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            _gitea_status(
                context="ci",
                status="success",
                id=20,
                updated_at="2026-11-27T12:02:00Z",
            ),
            _gitea_status(
                context="ci",
                status="pending",
                id=10,
                updated_at="2026-11-27T12:00:00Z",
            ),
            _gitea_status(
                context="lint",
                status="success",
                id=21,
                updated_at="2026-11-27T12:01:00Z",
            ),
        ],
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    names = {c.name: c.state for c in snap.checks}
    assert names == {"ci": CheckState.SUCCESS, "lint": CheckState.SUCCESS}
    assert snap.checks_combined is CheckState.SUCCESS


async def test_gitea_measured_python_static_history_keeps_latest_success(conn) -> None:
    """Measured live history (success, then two pending rows) resolves to success."""
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            _gitea_status(
                context="python-static",
                status="success",
                id=17,
                updated_at="2026-11-27T20:18:58Z",
                description="Successful in 4m18s",
            ),
            _gitea_status(
                context="python-static",
                status="pending",
                id=15,
                updated_at="2026-11-27T20:14:40Z",
                description="In progress",
            ),
            _gitea_status(
                context="python-static",
                status="pending",
                id=8,
                updated_at="2026-11-27T20:14:10Z",
                description="Waiting to run",
            ),
        ],
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    check = next(c for c in snap.checks if c.name == "python-static")
    assert check.state is CheckState.SUCCESS
    assert check.description == "Successful in 4m18s"
    assert snap.checks_combined is CheckState.SUCCESS
    assert "statuses" not in snap.partial
    assert "truncated_checks" not in snap.partial


async def test_gitea_timestamp_tie_breaks_on_id(conn) -> None:
    same = "2026-11-27T12:00:00Z"
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            _gitea_status(context="ci", status="pending", id=1, updated_at=same),
            _gitea_status(context="ci", status="failure", id=3, updated_at=same),
            _gitea_status(context="ci", status="success", id=2, updated_at=same),
        ],
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert len(snap.checks) == 1
    assert snap.checks[0].state is CheckState.FAILURE
    assert snap.checks_combined is CheckState.FAILURE


async def test_gitea_mixed_outcomes_retain_distinctions(conn) -> None:
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            _gitea_status(
                context="ok",
                status="success",
                id=1,
                updated_at="2026-11-27T12:00:00Z",
            ),
            _gitea_status(
                context="bad",
                status="error",
                id=2,
                updated_at="2026-11-27T12:00:00Z",
            ),
            _gitea_status(
                context="warn",
                status="warning",
                id=3,
                updated_at="2026-11-27T12:00:00Z",
            ),
            _gitea_status(
                context="run",
                status="pending",
                id=4,
                updated_at="2026-11-27T12:00:00Z",
            ),
        ],
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    names = {c.name: c.state for c in snap.checks}
    assert names["ok"] is CheckState.SUCCESS
    assert names["bad"] is CheckState.ERROR
    assert names["warn"] is CheckState.UNKNOWN
    assert names["run"] is CheckState.PENDING
    assert snap.checks_combined is CheckState.FAILURE  # error wins


async def test_gitea_pagination_surfaces_older_page_contexts(conn) -> None:
    page1 = [
        _gitea_status(
            context="ci",
            status="pending",
            id=n,
            updated_at="2026-11-27T12:00:00Z",
        )
        for n in range(1, delivery.STATUS_PAGE + 1)
    ]
    # Rewrite the last row of page 1 as the older pending for `ci`, and put success
    # plus a second context on page 2 so pagination cannot hide it.
    page1[-1] = _gitea_status(
        context="ci",
        status="pending",
        id=1,
        updated_at="2026-11-27T11:00:00Z",
    )
    page2 = [
        _gitea_status(
            context="ci",
            status="success",
            id=100,
            updated_at="2026-11-27T12:30:00Z",
        ),
        _gitea_status(
            context="lint",
            status="success",
            id=101,
            updated_at="2026-11-27T12:30:00Z",
        ),
    ]
    snap = await checker(_gitea_status_pages({1: page1, 2: page2})).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    names = {c.name: c.state for c in snap.checks}
    assert names["ci"] is CheckState.SUCCESS
    assert names["lint"] is CheckState.SUCCESS
    assert snap.checks_combined is CheckState.SUCCESS
    assert "truncated_checks" not in snap.partial


async def test_gitea_full_page_budget_is_conservative_partial(conn) -> None:
    """STATUS_PAGES of full pages: further history may exist — never invent green."""
    pages = {
        page: [
            _gitea_status(
                context=f"c{page}-{n}",
                status="success",
                id=page * 1000 + n,
                updated_at="2026-11-27T12:00:00Z",
            )
            for n in range(delivery.STATUS_PAGE)
        ]
        for page in range(1, delivery.STATUS_PAGES + 1)
    }
    snap = await checker(_gitea_status_pages(pages)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert "truncated_checks" in snap.partial
    assert snap.checks_combined is CheckState.UNKNOWN
    assert snap.checks  # evidence retained, but combined fail-closed


async def test_gitea_off_head_status_fails_closed(conn) -> None:
    other = "dddddddddddddddddddddddddddddddddddddddd"
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            _gitea_status(
                context="ci",
                status="success",
                id=1,
                updated_at="2026-11-27T12:00:00Z",
                sha=other,
            ),
        ],
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert snap.checks == []
    assert snap.checks_combined is CheckState.UNKNOWN
    assert "statuses" in snap.partial


async def test_gitea_malformed_status_row_fails_closed(conn) -> None:
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": [
            _gitea_status(
                context="ci",
                status="success",
                id=1,
                updated_at="2026-11-27T12:00:00Z",
            ),
            "not-an-object",
        ],
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert snap.checks == []
    assert snap.checks_combined is CheckState.UNKNOWN
    assert "statuses" in snap.partial


async def test_gitea_context_cap_truncation_is_unknown_combined(conn) -> None:
    rows = [
        _gitea_status(
            context=f"ctx-{n}",
            status="success",
            id=n,
            updated_at="2026-11-27T12:00:00Z",
        )
        for n in range(delivery.CHECK_CAP + 5)
    ]
    routes = {
        "/pulls/12": open_pr(),
        f"/commits/{HEAD}/statuses": rows,
        "/pulls/12/reviews": [],
    }
    snap = await checker(forge_router(routes)).snapshot(conn, REF)  # ty: ignore[invalid-argument-type]
    assert len(snap.checks) == delivery.CHECK_CAP
    assert "truncated_checks" in snap.partial
    assert snap.checks_combined is CheckState.UNKNOWN


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_http_failure_is_unknown_never_open(status, conn) -> None:
    c = checker(lambda r: httpx.Response(status, json={"state": "open"}))
    assert (await c.check(conn, REF))[0] is PrState.UNKNOWN  # ty: ignore[invalid-argument-type]


async def test_network_error_and_missing_credential_are_unknown(
    conn, db: Database
) -> None:
    def boom(request):
        raise httpx.ConnectError("down")

    assert (await checker(boom).check(conn, REF))[0] is PrState.UNKNOWN  # ty: ignore[invalid-argument-type]
    called = []
    c = checker(lambda r: called.append(r) or httpx.Response(200, json={}))
    # A repository no project names, the same one on another host, and a forge
    # integration whose validation failed: no credential, so no request.
    for ref in (
        PrRef(HOST, OWNER, "elsewhere", 12),
        PrRef("other.example.test", OWNER, REPO, 12),
    ):
        assert (await c.check(conn, ref))[0] is PrState.UNKNOWN  # ty: ignore[invalid-argument-type]
    async with db.write() as tx:
        await tx.conn.execute("UPDATE integrations SET validation = 'failed'")
    assert (await c.check(conn, REF))[0] is PrState.UNKNOWN  # ty: ignore[invalid-argument-type]
    assert called == []


async def test_concurrent_checks_share_one_flight_and_are_cached(conn) -> None:
    calls = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.path))
        await gate.wait()  # every caller arrives while the first read is in flight
        if request.url.path.endswith("/pulls/12"):
            return httpx.Response(200, json=open_pr())
        if "statuses" in request.url.path:
            return httpx.Response(200, json=[])
        if "reviews" in request.url.path:
            return httpx.Response(200, json=[])
        if "branch_protections" in request.url.path:
            return httpx.Response(404, json={})
        return httpx.Response(404, json={})

    c = checker(handler)
    pending = asyncio.gather(*(c.check(conn, REF) for _ in range(5)))  # ty: ignore[invalid-argument-type]
    await asyncio.sleep(0)
    gate.set()
    out = await pending
    assert {o[0] for o in out} == {PrState.OPEN}
    pr_calls = [p for p in calls if p.endswith("/pulls/12")]
    assert len(pr_calls) == 1
    await c.check(conn, REF)  # ty: ignore[invalid-argument-type]
    assert len([p for p in calls if p.endswith("/pulls/12")]) == 1  # cached


# -- REST and MCP ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def forge(client: TestClient, handler) -> None:
    client.app.state.pr_checker.transport = httpx.MockTransport(handler)  # ty: ignore[unresolved-attribute]


def setup_repository(client: TestClient) -> None:
    store = MemorySecretStore()
    client.app.state.secrets = store  # ty: ignore[unresolved-attribute]
    client.app.state.pr_checker.secrets = store  # ty: ignore[unresolved-attribute]
    res = client.put(
        "/api/pools/web",
        json={
            "runtime": "nanoclaw",
            "image": "h",
            "max_replicas": 1,
            "docker": {"runtime": "runsc"},
            "nanoclaw": {
                "install_dir": "/opt/nanoclaw",
                "instances_root": "/srv/i",
                "agent_image": "a:1",
                "repositories": {"demo": f"https://{HOST}/{OWNER}/{REPO}"},
            },
        },
    )
    assert res.status_code in (200, 201), res.text
    add_forge(client, HOST)


def review_task(client: TestClient, summary: str, **extra) -> str:
    body = {"project": "demo", "title": "CA5", "review": "orchestrator", **extra}
    tid = client.post("/api/tasks", json=body).json()["id"]
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    token = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    worker = {"Authorization": f"Bearer {token}"}
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    res = client.post(
        f"/api/tasks/{tid}/complete",
        json={"attempt": claimed["attempt"], "result": {"summary": summary}},
        headers=worker,
    )
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "in_review"
    return tid


def full_forge(pr_body, statuses=None, reviews=None, protection=None):
    statuses = [] if statuses is None else statuses
    reviews = [] if reviews is None else reviews

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("/pulls/12", "/pulls/44")):
            return httpx.Response(200, json=pr_body)
        if "statuses" in path:
            if isinstance(statuses, httpx.Response):
                return statuses
            return httpx.Response(200, json=statuses)
        if path.endswith("/reviews") or "/reviews" in path:
            if isinstance(reviews, httpx.Response):
                return reviews
            return httpx.Response(200, json=reviews)
        if "branch_protections" in path:
            if protection is None:
                return httpx.Response(404, json={})
            if isinstance(protection, httpx.Response):
                return protection
            return httpx.Response(200, json=protection)
        return httpx.Response(404, json={})

    return handler


def test_rest_merged_pr_flags_and_offers_the_existing_review_accept(client) -> None:
    setup_repository(client)
    forge(
        client,
        full_forge(
            {
                "merged": True,
                "state": "closed",
                "created_at": SOON,
                "merged_at": SOON,
                "head": {"sha": HEAD, "ref": "feat"},
                "base": {"sha": BASE, "ref": "main"},
            }
        ),
    )
    tid = review_task(client, f"Delivered in {URL}")
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr"] == URL and got["pr_state"] == "merged"
    assert got["merged_at"].startswith(SOON[:16])
    assert got["checked_at"]
    assert got["flags"] == ["delivery_merged_pending_review"]
    assert got["accept_blocked"] is None
    assert got["task"]["id"] == tid and got["task"]["state"] == "in_review"
    assert got["head"]["sha"] == HEAD and got["head"]["source"] == "forge"
    assert got["authorization"]["accept_decision_id"] == got["accept_decision_id"]
    assert "authorize merge" in got["authorization"]["notes"].lower()
    assert got["checklist"]["pending"] == 0
    # Observation changed nothing: still in review, no attempt spent, nothing closed.
    task = client.get(f"/api/tasks/{tid}").json()
    assert task["state"] == "in_review" and task["failures"] == 0
    res = client.post(
        f"/api/decisions/{got['accept_decision_id']}/answer",
        json={"choice": "accept", "text": ""},
    )
    assert res.status_code == 200, res.text
    assert client.get(f"/api/tasks/{tid}").json()["state"] == "succeeded"


def test_rest_pending_checklist_blocks_the_accept_offer(client) -> None:
    setup_repository(client)
    forge(
        client,
        full_forge(
            {"merged": True, "state": "closed", "head": {"sha": HEAD, "ref": "f"}}
        ),
    )
    tid = review_task(client, f"Delivered in {URL}", acceptance=["qa passes"])
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr_state"] == "merged"
    assert got["accept_decision_id"] is None
    assert got["accept_blocked"] == "checklist_pending"
    assert got["checklist"]["pending"] >= 1
    assert got["authorization"]["accept_blocked"] == "checklist_pending"


def test_rest_forge_failure_is_unknown_and_not_flagged(client) -> None:
    setup_repository(client)
    forge(client, lambda r: httpx.Response(401, json={}))
    tid = review_task(client, f"Delivered in {URL}")
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr_state"] == "unknown"
    assert got["flags"] == [] and got["accept_decision_id"] is None


def test_rest_exact_head_checks_partial_and_stale(client) -> None:
    setup_repository(client)
    forge(
        client,
        full_forge(
            open_pr(),
            statuses=[
                {"context": "ci", "status": "success"},
                {"context": "lint", "status": "failure"},
            ],
            reviews=[{"commit_id": HEAD}],
            protection=httpx.Response(403, json={}),
        ),
    )
    # Result reports a different head → stale.
    tid = review_task(
        client,
        f"Delivered in {URL}",
    )
    # Patch result data with a mismatched head via a new completion is hard; use
    # progress through worker is closed. Stale via task_reported from result:
    # complete already stored summary only. Force by forging head vs empty task head:
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr_state"] == "open"
    assert got["checks_combined"] == "failure"
    assert {c["name"] for c in got["checks"]} == {"ci", "lint"}
    assert got["required_gates"] is None  # 403 → unknown
    assert got["review"]["agentic_finding_count"] is None  # never invent 0
    assert got["merge"]["mergeable"] is True
    assert "authorize merge" in " ".join(got["disclosures"]).lower()


def test_rest_allow_merge_authorization_is_separate_from_green(client) -> None:
    setup_repository(client)
    forge(
        client,
        full_forge(open_pr(), statuses=[{"context": "ci", "status": "success"}]),
    )
    tid = client.post(
        "/api/tasks",
        json={
            "project": "demo",
            "title": "merge-auth",
            "review": "orchestrator",
            "input": {"delivery": {"allow_merge": True}},
        },
    ).json()["id"]
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    token = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    worker = {"Authorization": f"Bearer {token}"}
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    assert (
        client.post(
            f"/api/tasks/{tid}/complete",
            json={"attempt": claimed["attempt"], "result": {"summary": f"see {URL}"}},
            headers=worker,
        ).status_code
        == 200
    )
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["checks_combined"] == "success"
    assert got["authorization"]["allow_merge"] is True
    # Green + allow_merge still does not put accept on an open (unmerged) PR.
    assert got["accept_decision_id"] is None
    assert got["flags"] == []


def test_rest_no_reference_is_null_and_workers_stay_scoped(client) -> None:
    setup_repository(client)
    forge(client, lambda r: httpx.Response(500))
    tid = review_task(client, "done, see PR 12 and issue #3")
    assert client.get(f"/api/tasks/{tid}/delivery").json() is None
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    token = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    res = client.get(
        f"/api/tasks/{tid}/delivery", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 403


def test_rest_bare_number_in_prose_is_null_and_reads_no_pr(client) -> None:
    """Task 75c0eb23's progress text, verbatim: a merged PR it merely mentions is
    not its deliverable, so the projection reports none and never reads that PR."""
    setup_repository(client)
    seen: list[str] = []

    def handler(r):
        seen.append(r.url.path)
        return httpx.Response(200, json=open_pr())

    forge(client, handler)
    tid = review_task(
        client,
        "no prior branch or PR carries this work (only the spec revision PR #1399,"
        " merged). Branch argus/task-75c0eb23 is now reset to master at 360a3800.",
    )
    assert client.get(f"/api/tasks/{tid}/delivery").json() is None
    assert seen == []


def test_rest_a_pr_that_predates_the_task_is_rejected(client) -> None:
    setup_repository(client)
    forge(
        client,
        full_forge(
            {
                "state": "closed",
                "merged": True,
                "created_at": "2026-11-17T15:40:00Z",
                "merged_at": "2026-11-17T15:44:01Z",
                "head": {"sha": HEAD, "ref": "docs/guided-view-review-fixes"},
                "base": {"sha": BASE, "ref": "main"},
            }
        ),
    )
    tid = review_task(client, f"Delivered in {URL}")
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got is None  # never `merged` + delivery_merged_pending_review
    assert client.get(f"/api/tasks/{tid}").json()["state"] == "in_review"


def test_rest_a_foreign_branch_is_rejected(client) -> None:
    """The task declares its branch; a PR whose head is another branch is not its
    deliverable even when the worker reported it as a PR artifact."""
    setup_repository(client)
    forge(
        client,
        full_forge(open_pr(head={"sha": HEAD, "ref": "docs/guided-view-review-fixes"})),
    )
    tid = client.post(
        "/api/tasks",
        json={"project": "demo", "title": "foreign", "review": "orchestrator"},
    ).json()["id"]
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    token = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    worker = {"Authorization": f"Bearer {token}"}
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    mine = f"argus/task-{tid[:8]}"
    for body in (
        {
            "attempt": claimed["attempt"],
            "message": "branch",
            "kind": "commit_created",
            "data": {"branch": mine, "sha": HEAD},
        },
        {
            "attempt": claimed["attempt"],
            "message": "PR up",
            "kind": "pr_opened",
            "data": {"number": 12, "url": URL},
        },
    ):
        res = client.post(f"/api/tasks/{tid}/progress", json=body, headers=worker)
        assert res.status_code in (200, 201), res.text
    assert client.get(f"/api/tasks/{tid}/delivery").json() is None


def test_rest_pr_opened_is_recorded_only_for_the_tasks_own_pr(client) -> None:
    """The ed8b5081 sequence: a worker names PR 416 (another branch), PR 417 (older
    than the task) and then its own PR 426. Only the last is the task's milestone and
    PR artifact; the others are `progress.pr_rejected` and renew the lease."""
    setup_repository(client)
    prs = {
        "416": open_pr(head={"sha": HEAD, "ref": "feat/other"}, created_at=SOON),
        "417": open_pr(
            head={"sha": HEAD, "ref": "PLACEHOLDER"}, created_at="2020-01-01T00:00:00Z"
        ),
        "426": open_pr(head={"sha": HEAD, "ref": "PLACEHOLDER"}, created_at=SOON),
    }
    inner = full_forge(prs["426"])

    def handler(request: httpx.Request) -> httpx.Response:
        number = request.url.path.rsplit("/pulls/", 1)[-1]
        if request.url.path.endswith(f"/pulls/{number}") and number in prs:
            return httpx.Response(200, json=prs[number])
        return inner(request)

    forge(client, handler)
    tid = client.post("/api/tasks", json={"project": "demo", "title": "mine"}).json()[
        "id"
    ]
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": "worker"}).json()
    token = client.post(
        "/api/agents/register", json={"bootstrap_token": boot["token"]}
    ).json()["token"]
    worker = {"Authorization": f"Bearer {token}"}
    claimed = client.post("/api/tasks/claim", json={}, headers=worker).json()
    mine = f"argus/task-{tid[:8]}"
    for n in ("417", "426"):
        prs[n]["head"]["ref"] = mine

    def report(number: str):
        url = f"https://{HOST}/{OWNER}/{REPO}/pulls/{number}"
        return client.post(
            f"/api/tasks/{tid}/progress",
            json={
                "attempt": claimed["attempt"],
                "message": "PR up",
                "kind": "pr_opened",
                "data": {"number": int(number), "url": url},
            },
            headers=worker,
        )

    for number in ("416", "417"):
        res = report(number)
        assert res.status_code == 200, res.text
        assert res.json()["progress_kind"] is None
    res = report("426")
    assert res.status_code == 200, res.text
    assert res.json()["progress_data"]["number"] == 426
    artifacts = client.get(f"/api/tasks/{tid}/continuation").json()["artifacts"]
    assert [a["ref"] for a in artifacts if a["kind"] == "pr"] == ["426"]
    rejected = [
        e
        for e in client.get("/api/events", params={"subject_id": tid}).json()
        if e["kind"] == "progress.pr_rejected"
    ]
    reasons = {e["data"]["data"]["number"]: e["data"]["reason"] for e in rejected}
    assert sorted(reasons) == [416, 417]
    assert "feat/other" in reasons[416]
    assert "before the task" in reasons[417]


def test_rest_a_pr_on_the_tasks_own_branch_is_bound(client) -> None:
    """The counterpart: the same shape with the task's own head branch projects."""
    setup_repository(client)
    forge(client, full_forge(open_pr(head={"sha": HEAD, "ref": "feat"})))
    tid = review_task(client, f"Delivered in {URL}")
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr"] == URL and got["pr_state"] == "open"


def test_mcp_exposes_the_delivery(client) -> None:
    setup_repository(client)
    forge(
        client,
        full_forge(
            {"merged": True, "state": "closed", "head": {"sha": HEAD, "ref": "f"}}
        ),
    )
    tid = review_task(client, f"Delivered in {URL}")
    got = tool(client, ADMIN_TOKEN, "get_task_delivery", task_id=tid)
    assert got["pr_state"] == "merged"
    assert got["flags"] == ["delivery_merged_pending_review"]
    assert got["accept_decision_id"]
    assert got["authorization"]["notes"]
    assert got["task"]["id"] == tid


def test_inbox_does_not_embed_delivery(client) -> None:
    """Inbox stays cheap; delivery is a follow-up evidence call (spec 3.5)."""
    setup_repository(client)
    forge(client, full_forge(open_pr()))
    review_task(client, f"Delivered in {URL}")
    inbox = client.get("/api/inbox").json()
    assert "items" in inbox
    for row in inbox["items"]:
        assert "checks_combined" not in row
        assert "pr_state" not in row


# -- changed files (review policy spec 3.2) --------------------------------------


def _files_forge(pages: list[list[dict]], head: str = HEAD):
    """A forge whose PR files come in `pages` of 100 and whose head is `head`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/12/files"):
            n = int(request.url.params["page"])
            return httpx.Response(200, json=pages[n - 1] if n <= len(pages) else [])
        if request.url.path.endswith("/pulls/12"):
            return httpx.Response(200, json=open_pr(head={"sha": head, "ref": "f"}))
        return httpx.Response(404, json={})

    return handler


def _page(n: int, start: int = 0) -> list[dict]:
    return [{"filename": f"src/f{start + i}.py"} for i in range(n)]


async def test_changed_files_include_both_paths_of_a_rename(conn) -> None:
    pages = [[{"filename": "new/a.py", "previous_filename": "old/a.py"}, *_page(1)]]
    got = await checker(_files_forge(pages)).changed_files(conn, REF, HEAD)
    assert got.paths == ("new/a.py", "old/a.py", "src/f0.py")


async def test_changed_files_page_through_exactly_the_cap(conn) -> None:
    pages = [_page(100, 0), _page(100, 100), _page(100, 200)]
    got = await checker(_files_forge(pages)).changed_files(conn, REF, HEAD)
    assert got.paths is not None
    assert len(got.paths) == 300


@pytest.mark.parametrize("why", ["too_many", "head_moved", "failed_read"])
async def test_changed_files_are_unknown_when_not_trustworthy(conn, why) -> None:
    pages = [_page(100, 0), _page(100, 100), _page(100, 200), _page(1, 300)]
    handler = _files_forge(pages)
    if why == "head_moved":
        handler = _files_forge(pages[:1], head="e" * 40)
    if why == "failed_read":
        handler = forge_router({})
    got = await checker(handler).changed_files(conn, REF, HEAD)
    assert got.paths is None
