"""PR delivery state of a task (docs/specs/task-delivery.spec.md): the reference
parser, the read-only forge check, REST and MCP. The forge is a mock transport."""

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import httpx2 as httpx
import pytest
from argus_api.main import app
from argus_api.secrets import MemorySecretStore
from argus_api.tasks import delivery
from argus_api.tasks.delivery import PrChecker, PrRef
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ContinuationArtifact,
    ContinuationProvenance,
)
from argus_core.delivery import PrState
from fastapi.testclient import TestClient
from test_mcp import ADMIN_TOKEN as MCP_ADMIN
from test_mcp import tool

HOST, OWNER, REPO = "git.example.test", "team", "argus"
REPOSITORY = (HOST, OWNER, REPO)
ALLOWED = {REPOSITORY, (HOST, "team", "radar")}
URL = f"https://{HOST}/{OWNER}/{REPO}/pulls/12"
ADMIN_TOKEN = MCP_ADMIN


def pr_artifact(ref: str, url: str | None = None, minute: int = 0):
    return ContinuationArtifact(
        kind=ArtifactKind.PR,
        ref=ref,
        url=url,
        state=ArtifactState.OPEN,
        provenance=ContinuationProvenance.WORKER,
        observed_at=datetime(2026, 10, 24, 10, minute, tzinfo=UTC),
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


def test_bare_number_needs_a_repository_the_project_proves() -> None:
    assert delivery.extract(["PR #12 is open"], [], None, ALLOWED) is None
    ref = delivery.extract(["PR #12 is open"], [], REPOSITORY, ALLOWED)
    assert ref == PrRef(HOST, OWNER, REPO, 12)
    ref = delivery.extract(["pull request #12"], [], REPOSITORY, ALLOWED)
    assert ref is not None


def test_loose_text_is_not_a_reference() -> None:
    for text in ("fixes #12", "see issue #12", "PR 12", "#12", "PR #x", "PR #1a"):
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


def test_repository_url_parsing() -> None:
    assert delivery.parse_repository(f"https://{HOST.upper()}/Team/Argus.git") == (
        HOST,
        "team",
        "argus",
    )
    assert delivery.parse_repository("http://x/a/b") is None
    assert delivery.parse_repository("https://x/a") is None


# -- forge check ----------------------------------------------------------------


class Conn:
    """An accounts query that returns one gitea secret reference."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_):
        rows = self.rows

        class Cur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def fetchall(self):
                return rows

        return Cur()


class Secrets:
    def get(self, name):
        return {"username": "bot", "token": "dummy-token"} if name == "g" else None


REF = PrRef(HOST, OWNER, REPO, 12)


def checker(handler) -> PrChecker:
    return PrChecker(Secrets(), httpx.MockTransport(handler))  # ty: ignore[invalid-argument-type]


CONN = Conn([("sops:g",)])


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
async def test_states_are_read_with_the_repository_credential(body, want) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    state, merged_at, _ = await checker(handler).check(CONN, REF)  # ty: ignore[invalid-argument-type]
    assert state is want
    assert (merged_at is not None) == (want is PrState.MERGED)
    [req] = seen
    assert req.method == "GET"
    assert str(req.url) == f"https://{HOST}/api/v1/repos/{OWNER}/{REPO}/pulls/12"
    assert req.headers["authorization"].startswith("Basic ")


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_http_failure_is_unknown_never_open(status) -> None:
    c = checker(lambda r: httpx.Response(status, json={"state": "open"}))
    assert (await c.check(CONN, REF))[0] is PrState.UNKNOWN  # ty: ignore[invalid-argument-type]


async def test_network_error_and_missing_credential_are_unknown() -> None:
    def boom(request):
        raise httpx.ConnectError("down")

    assert (await checker(boom).check(CONN, REF))[0] is PrState.UNKNOWN  # ty: ignore[invalid-argument-type]
    called = []
    c = checker(lambda r: called.append(r) or httpx.Response(200, json={}))
    assert (await c.check(Conn([]), REF))[0] is PrState.UNKNOWN  # ty: ignore[invalid-argument-type]
    assert called == []  # no credential: no request


async def test_concurrent_checks_share_one_request_and_are_cached() -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"state": "open", "merged": False})

    c = checker(handler)
    out = await asyncio.gather(*(c.check(CONN, REF) for _ in range(5)))  # ty: ignore[invalid-argument-type]
    assert {o[0] for o in out} == {PrState.OPEN}
    await c.check(CONN, REF)  # ty: ignore[invalid-argument-type]
    assert len(calls) == 1


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
                "repositories": {"global": f"https://{HOST}/{OWNER}/{REPO}"},
            },
        },
    )
    assert res.status_code in (200, 201), res.text
    res = client.post(
        "/api/accounts",
        json={
            "name": "repo",
            "provider": "gitea",
            "credential": {"username": "bot", "token": "dummy-token"},
        },
    )
    assert res.status_code == 201, res.text


def review_task(client: TestClient, summary: str, **extra) -> str:
    tid = client.post(
        "/api/tasks",
        json={"project": "global", "title": "CA5", "review": "orchestrator", **extra},
    ).json()["id"]
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


def test_rest_merged_pr_flags_and_offers_the_existing_review_accept(client) -> None:
    setup_repository(client)
    forge(
        client,
        lambda r: httpx.Response(
            200,
            json={
                "merged": True,
                "state": "closed",
                "merged_at": "2026-10-24T09:00:00Z",
            },
        ),
    )
    tid = review_task(client, f"Delivered in {URL}")
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr"] == URL and got["pr_state"] == "merged"
    assert got["merged_at"].startswith("2026-10-24T09:00:00")
    assert got["checked_at"]
    assert got["flags"] == ["delivery_merged_pending_review"]
    assert got["accept_blocked"] is None
    # Observation changed nothing: still in review, no attempt spent, nothing closed.
    task = client.get(f"/api/tasks/{tid}").json()
    assert task["state"] == "in_review" and task["failures"] == 0
    # Accepting is the ordinary review decision.
    res = client.post(
        f"/api/decisions/{got['accept_decision_id']}/answer",
        json={"choice": "accept", "text": ""},
    )
    assert res.status_code == 200, res.text
    assert client.get(f"/api/tasks/{tid}").json()["state"] == "succeeded"


def test_rest_pending_checklist_blocks_the_accept_offer(client) -> None:
    setup_repository(client)
    forge(
        client, lambda r: httpx.Response(200, json={"merged": True, "state": "closed"})
    )
    tid = review_task(client, f"Delivered in {URL}", acceptance=["qa passes"])
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr_state"] == "merged"
    assert got["accept_decision_id"] is None
    assert got["accept_blocked"] == "checklist_pending"


def test_rest_forge_failure_is_unknown_and_not_flagged(client) -> None:
    setup_repository(client)
    forge(client, lambda r: httpx.Response(401, json={}))
    tid = review_task(client, f"Delivered in {URL}")
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr_state"] == "unknown"
    assert got["flags"] == [] and got["accept_decision_id"] is None


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


def test_rest_bare_number_uses_the_project_repository(client) -> None:
    setup_repository(client)
    seen = []

    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(200, json={"state": "open", "merged": False})

    forge(client, handler)
    tid = review_task(client, "PR #44 is up")
    got = client.get(f"/api/tasks/{tid}/delivery").json()
    assert got["pr"] == f"https://{HOST}/{OWNER}/{REPO}/pulls/44"
    assert got["pr_state"] == "open" and got["flags"] == []
    assert seen == [f"https://{HOST}/api/v1/repos/{OWNER}/{REPO}/pulls/44"]


def test_mcp_exposes_the_delivery(client) -> None:
    setup_repository(client)
    forge(
        client, lambda r: httpx.Response(200, json={"merged": True, "state": "closed"})
    )
    tid = review_task(client, f"Delivered in {URL}")
    got = tool(client, ADMIN_TOKEN, "get_task_delivery", task_id=tid)
    assert got["pr_state"] == "merged"
    assert got["flags"] == ["delivery_merged_pending_review"]
    assert got["accept_decision_id"]
