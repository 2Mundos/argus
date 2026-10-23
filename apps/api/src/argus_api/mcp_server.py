"""MCP endpoint for agents (worker lifecycle 3.7), served at /mcp/.

Every tool is a thin call to the REST API, made in-process with the agent's own
bearer token. Scopes, validation, leases, and errors therefore behave exactly
as over HTTP; this module holds no state or logic of its own.

Connect with `Authorization: Bearer <agent token>` (or an admin token for the
orchestrator-style tools).
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from types import FunctionType
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx2 as httpx
from argus_core.auth import Scope
from argus_core.continuation import (
    ChecklistReport,
    ContinuationUpdate,
    ObservationRequest,
)
from argus_core.task_edit import TaskChanges
from argus_core.tasks import ExecutorModel, HarnessModel
from fastapi import FastAPI
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_REQUEST
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as AsgiScope

from argus_api.auth import authenticate
from argus_api.errors import UnauthorizedError

INSTRUCTIONS = """\
Argus coordinates a fleet of agents. As a worker: claim_task, then work on it while
calling heartbeat (renews your leases) and report_progress; finish with complete_task
or fail_task. Every task write needs the task's `attempt`; a 409 error means you no
longer hold the task, so stop working on it. If you need a human or orchestrator to
decide something, use ask_question (your task is paused until answered). Read the
knowledge pages listed with each claimed task before starting; search_knowledge for
more, and write_knowledge when you learn something others will need. Lease provider
credentials only after claiming a task, with runtime_providers, task_id and attempt.
If an account runs out of quota, report_account_exhausted (same claim and providers)
returns fallback metadata; call lease_account to receive the next credential.

As an orchestrator, work the inbox: get_inbox lists what waits (blocked, in review,
awaiting approval) with decision and artifact ids; list_decisions(view="detail") or
get_decision reads the questions; list_artifacts, verify_artifact and read_artifact
check evidence; answer_decision or answer_decisions acts (give each answer a
request_id, so retries are safe); get_tasks(ids) reads the effects back. edit_task changes a task's title, spec,
acceptance, priority, capabilities, pins or dependencies (pass its `version`). Lists
are paged: `total` counts every match, follow `next_cursor` while `has_more`. If a
tool you need is missing, say which and stop; do not script REST around it.

For the fleet: inspect_pool shows what each instance is proven to run (a fact's
`source`, or `unknown`); recycle_instance and drain_agent never interrupt claimed
work (recycle_instance(requeue=true) and release_task requeue it without costing an
attempt); admins change options with update_pool(expected_version=the pool's version).
wait_for_events(subject_type, subject_id) waits for the change to happen instead
of polling; then inspect_pool or get_tasks reads back the effect."""

server = MCPServer("argus", version="0.1.0", instructions=INSTRUCTIONS)

_app: FastAPI | None = None

# Who a tool is listed to (MCP operations 3.4). Listing is not access control:
# every call is still decided by the REST endpoint it forwards to.
AGENTS = frozenset({Scope.WORKER, Scope.ORCHESTRATOR})
MANAGERS = frozenset({Scope.ORCHESTRATOR, Scope.ADMIN})
ADMINS = frozenset({Scope.ADMIN})
ANYONE = frozenset(Scope)
audiences: dict[str, frozenset[Scope]] = {}


def tool[F: FunctionType](audience: frozenset[Scope]) -> Callable[[F], F]:
    def register(fn: F) -> F:
        audiences[fn.__name__] = audience
        return server.tool()(fn)

    return register


async def _list_tools_for_caller(
    ctx: ServerRequestContext[Any, Any], call_next: CallNext
) -> HandlerResult:
    """Answer `tools/list` with only the tools the caller's scope can use."""
    result = await call_next(ctx)
    # The SDK hands middleware the serialized result; tests pin that shape.
    if ctx.method != "tools/list" or not isinstance(result, dict):
        return result
    headers = getattr(ctx.request, "headers", None) or {}
    scheme, _, token = headers.get("authorization", "").partition(" ")
    assert _app is not None
    try:
        if scheme.lower() != "bearer" or not token:
            raise UnauthorizedError("missing bearer token")
        async with _app.state.db.read() as conn:
            scope = (await authenticate(conn, token, now=_app.state.db.now())).scope
    except UnauthorizedError as exc:
        raise MCPError(INVALID_REQUEST, f"401 unauthorized: {exc}") from exc
    tools = [t for t in result["tools"] if scope in audiences[t["name"]]]
    return {**result, "tools": tools}


server.middleware.append(_list_tools_for_caller)


def bind(app: FastAPI) -> None:
    """Point the tools at the REST app they call into (set once at import)."""
    global _app
    _app = app


class SlashAlias:
    """Serve `/mcp` as well as `/mcp/`: some MCP clients do not follow a
    redirect on POST."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: AsgiScope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


class Endpoint:
    """The ASGI app mounted at /mcp. The SDK's session manager can run only once,
    so each app lifespan builds a fresh one (`async with endpoint.running()`)."""

    def __init__(self) -> None:
        self._app: ASGIApp | None = None

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[None]:
        self._app = _build()
        async with server.session_manager.run():
            yield
        self._app = None

    async def __call__(self, scope: AsgiScope, receive: Receive, send: Send) -> None:
        if self._app is None:
            raise RuntimeError("MCP endpoint used outside the app lifespan")
        await self._app(scope, receive, send)


endpoint = Endpoint()


def allowed_mcp_hosts(public_url: str | None) -> tuple[list[str], list[str]]:
    """Host/Origin allowlist for MCP DNS-rebinding protection."""
    hosts = [
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
        "host.docker.internal",
        "host.docker.internal:*",
    ]
    origins = [
        "http://localhost",
        "http://localhost:*",
        "https://localhost",
        "https://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "https://127.0.0.1",
        "https://127.0.0.1:*",
        "http://[::1]",
        "http://[::1]:*",
        "https://[::1]",
        "https://[::1]:*",
        "http://host.docker.internal",
        "http://host.docker.internal:*",
        "https://host.docker.internal",
        "https://host.docker.internal:*",
    ]
    if public_url:
        parsed = urlsplit(public_url)
        host = parsed.hostname
        if host:
            if parsed.port:
                hosts.append(f"{host}:{parsed.port}")
                origins.append(f"{parsed.scheme}://{host}:{parsed.port}")
            else:
                hosts.append(host)
                hosts.append(f"{host}:*")
                origins.append(f"{parsed.scheme}://{host}")
                origins.append(f"{parsed.scheme}://{host}:*")
            # Containers often reach a local Argus as host.docker.internal:PORT.
            if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                hosts.append(f"host.docker.internal:{port}")
                origins.append(f"{parsed.scheme}://host.docker.internal:{port}")
    return hosts, origins


def _build() -> ASGIApp:
    public_url = None
    if _app is not None and hasattr(_app.state, "settings"):
        public_url = _app.state.settings.public_url
    hosts, origins = allowed_mcp_hosts(public_url)
    return server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )


async def _call(
    ctx: Context,
    method: str,
    path: str,
    *,
    json: Any = None,
    params: dict[str, Any] | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    raw = ((ctx.headers or {}).get("authorization") or "").strip()
    if not raw:
        raise ToolError(
            "missing token: connect with the header 'Authorization: Bearer <token>'"
        )
    # The REST layer authenticates through HTTPBearer, which requires the scheme.
    # Forward the header verbatim, only adding "Bearer " when the client sent a
    # bare token, so the downstream call is authenticated instead of rejected.
    auth = raw if raw.lower().startswith("bearer ") else f"Bearer {raw}"
    assert _app is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app), base_url="http://argus", timeout=60
    ) as client:
        res = await client.request(
            method,
            "/api" + path,
            json=json,
            params={k: v for k, v in (params or {}).items() if v is not None},
            content=content,
            headers={"Authorization": auth, **(headers or {})},
        )
    if not res.content:
        body = None
    elif res.headers.get("content-type", "").startswith("text/plain"):
        body = res.text
    else:
        body = res.json()
    if res.is_error:
        raise ToolError(_describe(res.status_code, body))
    return body


def _describe(status: int, body: Any) -> str:
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        text = f"{status} {detail.get('code')}: {detail.get('message')}"
        if detail.get("details"):
            text += " | " + "; ".join(map(str, detail["details"]))
        return text
    if isinstance(detail, list):  # request validation
        return f"{status} invalid arguments: " + "; ".join(
            f"{'.'.join(map(str, e.get('loc', [])[1:]))}: {e.get('msg')}"
            for e in detail
        )
    return f"{status}: {detail}"


class Lease(BaseModel):
    task_id: str
    attempt: int


class Runtime(BaseModel):
    worker_version: str | None = None
    engine: str | None = None
    executor: str | None = None


ViewArg = Literal["summary", "detail"]
InboxState = Literal["blocked", "in_review", "awaiting_approval", "pending"]
TaskStateArg = Literal[
    "awaiting_approval",
    "pending",
    "ready",
    "claimed",
    "blocked",
    "in_review",
    "succeeded",
    "failed",
    "cancelled",
]
DecisionKindArg = Literal["question", "approval", "review", "policy", "knowledge"]
ArtifactRole = Literal[
    "report", "delegate_report", "policy_snapshot", "qa_receipt", "other"
]


# -- working on tasks ---------------------------------------------------------------


@tool(AGENTS)
async def claim_task(
    ctx: Context,
    capabilities: list[str] | None = None,
    wait_s: float = 20,
    runtime_providers: list[str] | None = None,
) -> dict:
    """Claim the next task you can do, waiting up to wait_s seconds (max 30) for one.
    `runtime_providers` are the account providers you can apply; a task restricted to
    other providers (account_providers) is skipped and stays ready.
    Returns the task (keep its `id` and `attempt`) with `knowledge_refs`: pages to
    read first. Returns {"task": null} when nothing became ready."""
    task = await _call(
        ctx,
        "POST",
        "/tasks/claim",
        json={
            "capabilities": capabilities or [],
            "wait_s": wait_s,
            "runtime_providers": runtime_providers or [],
        },
    )
    return {"task": task}


@tool(AGENTS)
async def heartbeat(
    ctx: Context, leases: list[Lease] | None = None, runtime: Runtime | None = None
) -> dict:
    """Prove you are alive and renew the leases on tasks you hold. Call it at least
    every heartbeat_interval_s (30 s). If a lease comes back rejected, stop working on
    that task. If state is "draining", finish your task and then call leave.
    `runtime` reports what you run (worker_version, engine, executor), if known."""
    body: dict[str, Any] = {"leases": [lease.model_dump() for lease in leases or []]}
    if runtime is not None:
        body["runtime"] = runtime.model_dump()
    return await _call(ctx, "POST", "/agents/me/heartbeat", json=body)


@tool(ANYONE)
async def get_task(ctx: Context, task_id: str) -> dict:
    """Read a task: spec, acceptance criteria, state, attempt, clarifications."""
    return await _call(ctx, "GET", f"/tasks/{task_id}")


@tool(ANYONE)
async def get_task_log(
    ctx: Context,
    task_id: str,
    attempt: int | None = None,
    after: int | None = None,
    limit: int = 200,
) -> dict:
    """Read the live output log of a task attempt (default: the current one), as the
    dashboard's "Ao vivo" panel shows it: {attempt, lines: [{seq, source, at, line}],
    last_seq}. Without `after` you get the newest `limit` lines (max 1000); pass
    `after`=last_seq to read on. Secrets are redacted. Workers read leased tasks only."""
    return await _call(
        ctx,
        "GET",
        f"/tasks/{task_id}/log",
        params={"attempt": attempt, "after": after, "limit": limit},
    )


@tool(ANYONE)
async def get_tasks(ctx: Context, ids: list[str], view: ViewArg = "summary") -> dict:
    """Read up to 50 tasks in one call, in the order given; unknown ids come back in
    `missing`. view=detail adds spec and result (long text cut, see `truncated`)."""
    return await _call(
        ctx, "GET", "/tasks", params={"ids": ",".join(ids), "view": view}
    )


@tool(ANYONE)
async def list_artifacts(
    ctx: Context,
    task_id: str,
    attempt: int | None = None,
    role: ArtifactRole | None = None,
) -> list[dict]:
    """A task's artifacts: name, attempt, role, size, sha256, and `in_result` (the
    task's result lists it). Metadata only; read_artifact for content."""
    return await _call(
        ctx,
        "GET",
        f"/tasks/{task_id}/artifacts",
        params={"attempt": attempt, "role": role},
    )


@tool(ANYONE)
async def read_artifact(
    ctx: Context, artifact_id: str, offset: int = 0, max_bytes: int = 16384
) -> dict:
    """Read a text or JSON artifact as UTF-8 from byte `offset` (max_bytes ≤ 65536);
    continue from `next_offset` until it is null. Other media types: `binary` true."""
    return await _call(
        ctx,
        "GET",
        f"/artifacts/{artifact_id}/text",
        params={"offset": offset, "max_bytes": max_bytes},
    )


@tool(ANYONE)
async def verify_artifact(ctx: Context, artifact_id: str) -> dict:
    """Re-hash an artifact's stored bytes against its recorded size and SHA-256.
    `ok` means the bytes are intact, not that the content is acceptable."""
    return await _call(ctx, "POST", f"/artifacts/{artifact_id}/verify")


@tool(AGENTS)
async def report_progress(
    ctx: Context,
    task_id: str,
    attempt: int,
    message: str,
    percent: int | None = None,
    kind: str | None = None,
    data: dict | None = None,
) -> dict:
    """Post a short progress note shown to humans (at most one per 10 s). It also
    renews your lease on the task. Optional `kind` marks a milestone: commit_created
    {branch, sha}, pr_opened {number, url}, ci_running|ci_passed|ci_failed {sha},
    gate_failed {gate}, waiting_input; a milestone is not limited unless its kind repeats."""
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/progress",
        json={
            "attempt": attempt,
            "message": message,
            "percent": percent,
            "kind": kind,
            "data": data,
        },
    )


@tool(AGENTS)
async def report_task_models(
    ctx: Context,
    task_id: str,
    attempt: int,
    executions: list[dict] | None = None,
    selections: list[dict] | None = None,
) -> dict:
    """Report which models ran for your task attempt, in two separate layers:
    `harness` (your agent runtime) and `delegate` (a CLI implementer). Each execution
    has a unique `key`, `layer`, `state` (selected|running|succeeded|failed|cancelled)
    and optional runtime/engine/executor/provider/requested_model/configured_model/
    observed_model + `evidence` (reported_by_runtime|cli_output|provider_response;
    never claim an observed model from config), policy_revision, account_id,
    delegation_id, run_key, resumed_from, error_code, started_at (omit if the model
    never started), ended_at. Each selection has `key`, `layer`, `kind` (initial|
    account_switch|model_switch|provider_switch|executor_switch|retry_no_fallback),
    `reason_code` (priority|availability|compatibility|context|quota|error|policy),
    a short `explanation`, from_*/to_* model/provider/executor/account_id,
    alternatives, execution_key, outcome. Re-sending a key completes it; it never
    overwrites. Never include credentials or prompts."""
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/models",
        json={
            "attempt": attempt,
            "executions": executions or [],
            "selections": selections or [],
        },
    )


@tool(ANYONE)
async def get_task_models(ctx: Context, task_id: str) -> dict:
    """Which models ran for a task, per layer (harness vs delegate): every execution
    across attempts with requested/configured/observed model and evidence, plus the
    selection and fallback timeline. An unreported model is null / not_reported."""
    return await _call(ctx, "GET", f"/tasks/{task_id}/models")


@tool(ANYONE)
async def get_agent_models(ctx: Context, agent_id: str) -> dict:
    """A worker's latest model per layer and its recent runs, with evidence."""
    return await _call(ctx, "GET", f"/agents/{agent_id}/models")


@tool(AGENTS)
async def complete_task(
    ctx: Context,
    task_id: str,
    attempt: int,
    summary: str,
    data: dict | None = None,
    artifacts: list[str] | None = None,
    account_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    checklist: list[ChecklistReport] | None = None,
) -> dict:
    """Finish the task. `summary` says what you did (Markdown); `data` must match the
    task kind's result schema if it has one; `artifacts` are ids from
    upload_text_artifact. Report token usage and the account you used, if known.
    `checklist` reports each acceptance item of the task's continuation contract by id
    (status done needs evidence); any item not done sends the result to review instead
    of success."""
    usage = {
        "account_id": account_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }
    result: dict[str, Any] = {
        "summary": summary,
        "data": data,
        "artifacts": artifacts or [],
        "checklist": [c.model_dump(mode="json") for c in checklist or []],
    }
    if any(v is not None for v in usage.values()):
        result["usage"] = usage
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/complete",
        json={"attempt": attempt, "result": result},
    )


@tool(AGENTS)
async def fail_task(ctx: Context, task_id: str, attempt: int, error: str) -> dict:
    """Give up on the task, explaining why. It is retried (by you or another agent)
    until it runs out of attempts."""
    return await _call(
        ctx, "POST", f"/tasks/{task_id}/fail", json={"attempt": attempt, "error": error}
    )


@tool(AGENTS)
async def ask_question(
    ctx: Context,
    task_id: str,
    attempt: int,
    question: str,
    options: list[str] | None = None,
    audience: Literal["any", "orchestrator", "human"] = "any",
    category: Literal["decision", "assistance"] = "decision",
    resource: str | None = None,
) -> dict:
    """Ask for a real choice of scope, cost or permission that you cannot make yourself.
    Your task is paused (its lease is released) until someone answers; the answer is
    recorded as an effective decision and in the spec's "Clarifications" history, and
    the task becomes claimable again. Never ask whether to run a gate, accept a partial
    result or verify merged work: resolve facts within your authorization.
    A missing tool, credential or service is `category="assistance"` with the `resource`
    name: it goes to the orchestrator (never a human) and is not a decision about the
    goal. 409 already_answered: the question has an effective answer (in the message)."""
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/ask",
        json={
            "attempt": attempt,
            "question": question,
            "options": options,
            "audience": audience,
            "category": category,
            "resource": resource,
        },
    )


@tool(ANYONE)
async def get_task_continuation(ctx: Context, task_id: str) -> dict:
    """The continuation contract of a task: phase (implement | resume | reconcile | qa |
    close_evidence), known branch/PR/commit artifacts with provenance and observation
    time, preserved WIP, the last interruption, the checklist with evidence, effective
    decisions (newest first) and `gaps` (what Argus does not know: unknown is not
    absent). Merged is not accepted: pending items stay pending."""
    return await _call(ctx, "GET", f"/tasks/{task_id}/continuation")


@tool(AGENTS)
async def report_observations(
    ctx: Context, task_id: str, attempt: int, observations: ObservationRequest
) -> dict:
    """Report what you saw of the live system for the task you hold: artifact state
    (PR open/merged/closed, head sha), environment readiness per resource
    (installed, credential_usable, reachable, permitted, validated) and preserved WIP.
    Idempotent; Argus stores it with runtime provenance and flags contradictions."""
    body = observations.model_dump(mode="json", exclude_none=True)
    body["attempt"] = attempt
    return await _call(ctx, "POST", f"/tasks/{task_id}/observations", json=body)


@tool(MANAGERS)
async def update_task_continuation(
    ctx: Context, task_id: str, update: ContinuationUpdate
) -> dict:
    """Settle continuation facts the runtime cannot: phase, artifacts, WIP, checklist
    items and evidence, environment status, decisions to retire. Pass the contract's
    `revision` as expected_revision (409 stale_continuation otherwise). Recorded with
    orchestrator provenance and audited."""
    return await _call(
        ctx,
        "PATCH",
        f"/tasks/{task_id}/continuation",
        json=update.model_dump(mode="json", exclude_none=True),
    )


@tool(ANYONE)
async def create_task(
    ctx: Context,
    title: str,
    project: str | None = None,
    spec: str = "",
    kind: str = "generic",
    parent_id: str | None = None,
    depends_on: list[str] | None = None,
    required_capabilities: list[str] | None = None,
    priority: int = 0,
    acceptance: list[str] | None = None,
    knowledge: list[str] | None = None,
    difficulty: Literal["trivial", "simple", "standard", "hard"] | None = None,
    difficulty_source: Literal["declared", "inferred"] | None = None,
    complexity: Literal["trivial", "standard", "hard"] | None = None,
    account_providers: list[str] | None = None,
    harness_model: HarnessModel | None = None,
    executor_model: ExecutorModel | None = None,
    harness_account: str | None = None,
) -> dict:
    """Create a task. Workers may only create subtasks of a task they hold (set
    parent_id; the project is inherited). Orchestrators create any task and must name
    a project. `knowledge` pins pages the claiming agent should read. `difficulty`:
    "trivial" (a copy change, a typo) or "simple" take the cheap Cursor executor
    (composer-2.5), trivial first on the node's local model with the cloud as the
    fallback; "standard" (the default) the Claude or Codex executor; "hard"
    (architecture, data migration, security, several services) the strong model (Opus).
    Omit it and Argus infers one conservatively from the title and spec.
    `difficulty_source` is "declared" by default; send "inferred" when the level is
    your suggestion (editable later). A failed attempt for context overflow or a
    failed verification raises it one level. `complexity` is the deprecated name
    (trivial | standard | hard) and is used only when `difficulty` is absent.
    `account_providers` (e.g. ["local"]) restricts the task to those engine account
    providers; leave it out for no restriction. `harness_model` ({engine: claude |
    codex, model}) and `executor_model` ({executor: claude | codex | agent, model})
    pin the engine/executor and its model for every attempt, with no fallback chain;
    the task then only runs on that provider's accounts. Leave them out to follow
    the model policy. `harness_account` (an account id or name, e.g. "strata-local"
    or a custom OpenRouter account) pins the engine lease to that one account: the
    task waits as awaiting_quota while it cannot serve and never runs on another.
    Local and custom accounts bring their own model, so pair them with no
    harness_model; a local (Strata) harness delegates only to the Cursor agent."""
    body: dict[str, Any] = {
        "title": title,
        "project": project,
        "spec": spec,
        "kind": kind,
        "parent_id": parent_id,
        "depends_on": depends_on or [],
        "priority": priority,
        "acceptance": acceptance or [],
        "knowledge": knowledge or [],
    }
    if required_capabilities is not None:
        body["required_capabilities"] = required_capabilities
    if difficulty is not None or complexity is not None:
        body["difficulty"] = difficulty or complexity
    if difficulty_source is not None:
        body["difficulty_source"] = difficulty_source
    if account_providers is not None:
        body["account_providers"] = account_providers
    if harness_model is not None:
        body["harness_model"] = harness_model.model_dump()
    if executor_model is not None:
        body["executor_model"] = executor_model.model_dump()
    if harness_account is not None:
        body["harness_account"] = harness_account
    return await _call(ctx, "POST", "/tasks", json=body)


@tool(AGENTS)
async def upload_text_artifact(
    ctx: Context,
    task_id: str,
    attempt: int,
    name: str,
    content: str,
    media_type: str = "text/plain",
    role: ArtifactRole | None = None,
) -> dict:
    """Attach a text file (report, diff, log, notes) to the task you hold. Pass the
    returned id in complete_task's `artifacts`. Names: letters, digits, . _ -
    `role`: report (yours), delegate_report (a delegate's original), policy_snapshot,
    qa_receipt, other."""
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/artifacts",
        params={"attempt": attempt, "name": name, "role": role},
        content=content.encode(),
        headers={"Content-Type": media_type},
    )


@tool(AGENTS)
async def leave(ctx: Context) -> dict:
    """End your identity cleanly (when drained or shutting down). Any task you still
    hold is released for someone else."""
    return await _call(ctx, "POST", "/agents/me/leave")


# -- knowledge ---------------------------------------------------------------------------


@tool(ANYONE)
async def search_knowledge(
    ctx: Context, query: str, project: str | None = None, limit: int = 10
) -> list[dict]:
    """Search the shared knowledge base (titles, summaries, bodies, tags). Stale pages
    are marked and ranked last; verify them before relying on them."""
    return await _call(
        ctx,
        "GET",
        "/knowledge/search",
        params={"q": query, "project": project, "limit": limit},
    )


@tool(ANYONE)
async def get_knowledge_page(
    ctx: Context, page_id: str, revision: int | None = None
) -> dict:
    """Read a knowledge page (Markdown body). Keep `revision_id` if you plan to edit it."""
    return await _call(
        ctx, "GET", f"/knowledge/pages/{page_id}", params={"revision": revision}
    )


@tool(ANYONE)
async def write_knowledge(
    ctx: Context,
    title: str,
    body: str,
    reason: str,
    summary: str = "",
    tags: list[str] | None = None,
    project: str = "global",
    page_id: str | None = None,
    base_revision_id: str | None = None,
) -> dict:
    """Record something others will need. Without page_id this creates a page; with
    page_id and base_revision_id (from get_knowledge_page) it edits it, and fails with
    knowledge_conflict if someone changed it meanwhile (re-read and retry). Depending
    on settings, your change is published or proposed for review (see `status`)."""
    content = {
        "title": title,
        "body": body,
        "summary": summary,
        "tags": tags or [],
        "reason": reason,
    }
    if page_id is None:
        return await _call(
            ctx, "POST", "/knowledge/pages", json={**content, "project": project}
        )
    if base_revision_id is None:
        raise ToolError("editing needs base_revision_id: read the page first")
    return await _call(
        ctx,
        "POST",
        f"/knowledge/pages/{page_id}/revisions",
        json={**content, "base_revision_id": base_revision_id},
    )


# -- provider accounts --------------------------------------------------------------------


@tool(MANAGERS)
async def list_accounts(ctx: Context) -> dict:
    """Provider accounts with what each reports about its quota. Per account:
    `quota` (provider windows: used_percent is the share USED, with reset and
    observed_at), `credit` (money left on a key, in USD, never tokens),
    `quota_status` (state known|stale|unknown, a `reason` code saying why a figure
    is missing, the last probe's outcome and code) and token usage. A missing figure
    is unknown, never zero. Read-only; no credentials."""
    return {"accounts": await _call(ctx, "GET", "/accounts")}


@tool(MANAGERS)
async def refresh_account_quota(ctx: Context, account_id: str) -> dict:
    """Read one account's quota from its provider now (a read-only request; at most
    once per account per 5 minutes; never refreshes the login). Returns the outcome
    (updated|skipped|rate_limited|failed, with a `code`), the stored windows and
    credit, and the account's resulting `quota_status`."""
    return await _call(ctx, "POST", f"/accounts/{account_id}/quota/refresh")


@tool(AGENTS)
async def lease_account(
    ctx: Context,
    runtime_providers: list[str],
    task_id: str,
    attempt: int,
) -> dict:
    """Get a provider account for a currently claimed task.

    `runtime_providers` is required and lists providers this runtime can apply;
    `task_id` and `attempt` must identify your live task claim. The best eligible
    account is selected by priority."""
    return await _call(
        ctx,
        "POST",
        "/accounts/lease",
        json={
            "runtime_providers": runtime_providers,
            "task_id": task_id,
            "attempt": attempt,
        },
    )


@tool(AGENTS)
async def report_account_exhausted(
    ctx: Context,
    account_id: str,
    runtime_providers: list[str],
    task_id: str,
    attempt: int,
    reason: str = "",
    until: str | None = None,
) -> dict:
    """Mark your leased account exhausted for a live claimed task.

    Returns fallback account metadata only. Obtain any next credential with
    `lease_account`; `runtime_providers` and the live claim are required.
    `until` is the provider's reset time (ISO 8601), if known."""
    return await _call(
        ctx,
        "POST",
        f"/accounts/{account_id}/exhausted",
        json={
            "reason": reason,
            "until": until,
            "runtime_providers": runtime_providers,
            "task_id": task_id,
            "attempt": attempt,
        },
    )


# -- orchestration ---------------------------------------------------------------------------


@tool(MANAGERS)
async def get_inbox(
    ctx: Context,
    project: str | None = None,
    states: list[InboxState] | None = None,
    decision_kinds: list[DecisionKindArg] | None = None,
    view: ViewArg = "summary",
    limit: int = 25,
    cursor: str | None = None,
) -> dict:
    """What waits on you, oldest first: blocked, in-review and awaiting-approval
    tasks with their open decision (id, kind, audience) and current artifact ids,
    pending tasks stuck on a failed or cancelled dependency (`failed_dependencies`:
    retry it or set_task_dependencies), plus open pool and knowledge decisions. Totals by state and decision kind
    cover every match. view=detail adds each task and decision in full."""
    return await _call(
        ctx,
        "GET",
        "/inbox",
        params={
            "project": project,
            "state": states,
            "decision_kind": decision_kinds,
            "view": view,
            "limit": limit,
            "cursor": cursor,
        },
    )


@tool(MANAGERS)
async def list_tasks(
    ctx: Context,
    states: list[TaskStateArg] | None = None,
    project: str | None = None,
    kind: str | None = None,
    assignee: str | None = None,
    view: ViewArg = "summary",
    limit: int = 25,
    cursor: str | None = None,
) -> dict:
    """List tasks, newest first, in pages (limit ≤ 100). Summary rows carry state
    and ids (decisions, artifacts, dependencies), not the spec or result.
    `assignee` (an agent id) narrows to the tasks that agent holds now."""
    return await _call(
        ctx,
        "GET",
        "/tasks",
        params={
            "state": states,
            "project": project,
            "kind": kind,
            "assignee": assignee,
            "view": view,
            "limit": limit,
            "cursor": cursor,
        },
    )


@tool(MANAGERS)
async def edit_task(
    ctx: Context, task_id: str, expected_version: str, changes: TaskChanges
) -> dict:
    """Edit a task's definition: title, spec, acceptance, priority,
    required_capabilities, knowledge (pinned page ids), depends_on,
    account_providers, difficulty (trivial | simple | standard | hard; not clearable),
    harness_model ({engine, model}), executor_model ({executor, model}) and
    harness_account (an account id or name). `changes` is a partial patch: a field
    you leave out is kept, null clears spec, acceptance, required_capabilities,
    knowledge, depends_on, account_providers (no restriction), a model pin (back to
    the policy) or the harness account pin (any account). Pass the task's `version` as
    expected_version (409 stale_version carries the current one). The task's
    `editable_fields` says what its state allows: ready, pending and
    awaiting_approval tasks take everything; blocked, claimed and in_review only
    priority; finished ones nothing (409 task_not_editable). Returns the task with
    its new `version`. Nothing is written when any field is refused."""
    return await _call(
        ctx,
        "PATCH",
        f"/tasks/{task_id}",
        json={
            "expected_version": expected_version,
            "changes": changes.model_dump(mode="json", exclude_unset=True),
        },
    )


@tool(MANAGERS)
async def set_task_dependencies(
    ctx: Context, task_id: str, depends_on: list[str], expected_version: str
) -> dict:
    """Replace a waiting task's dependencies (pending, awaiting_approval or ready);
    pass its `version` as expected_version. Drop a failed or cancelled dependency
    to release a stuck task. Cycles are refused (422 dependency_cycle)."""
    return await _call(
        ctx,
        "PUT",
        f"/tasks/{task_id}/dependencies",
        json={"depends_on": depends_on, "expected_version": expected_version},
    )


@tool(MANAGERS)
async def unblock_task(ctx: Context, task_id: str) -> dict:
    """Return a blocked task to ready without answering its question (the question
    is cancelled). To answer it, use answer_decision instead."""
    return await _call(ctx, "POST", f"/tasks/{task_id}/unblock")


@tool(MANAGERS)
async def retry_task(ctx: Context, task_id: str) -> dict:
    """Give a failed task a fresh window of attempts (409 task_not_failed otherwise)."""
    return await _call(ctx, "POST", f"/tasks/{task_id}/retry")


@tool(MANAGERS)
async def release_task(
    ctx: Context, task_id: str, reason: str, attempt: int | None = None
) -> dict:
    """Take a claimed task back from its worker and requeue it without counting a
    failed attempt (e.g. to free a worker for a recycle). `reason` is required;
    pass `attempt` to release only that claim (409 stale_attempt otherwise). 409
    task_not_claimed if it is not claimed."""
    body: dict = {"reason": reason}
    if attempt is not None:
        body["attempt"] = attempt
    return await _call(ctx, "POST", f"/tasks/{task_id}/release", json=body)


@tool(MANAGERS)
async def cancel_task(ctx: Context, task_id: str) -> dict:
    """Cancel a task (orchestrators and admins)."""
    return await _call(ctx, "POST", f"/tasks/{task_id}/cancel")


@tool(MANAGERS)
async def list_agents(ctx: Context, state: str | None = None) -> list[dict]:
    """List agents (active, draining, gone, lost) and how many tasks each holds."""
    return await _call(ctx, "GET", "/agents", params={"state": state})


@tool(MANAGERS)
async def list_pools(ctx: Context) -> list[dict]:
    """List worker pools with desired/max replicas and live instances. `env` shows
    keys only; values are write-only."""
    return await _call(ctx, "GET", "/pools")


@tool(MANAGERS)
async def inspect_pool(ctx: Context, name: str) -> dict:
    """A pool with its live instances and agents, and per instance what is proven
    about what runs: image, image_digest, worker_version, capabilities, engine,
    executor, ready. Each fact has a `source` (configured < requested < started <
    validated) or `unknown`: never read unknown as success."""
    return await _call(ctx, "GET", f"/pools/{name}/inspect")


@tool(ADMINS)
async def update_pool(
    ctx: Context,
    name: str,
    expected_version: str,
    image: str | None = None,
    capabilities: list[str] | None = None,
    desired_replicas: int | None = None,
    max_replicas: int | None = None,
    agents_per_instance: int | None = None,
    env: dict[str, str | None] | None = None,
    cpus: float | None = None,
    memory_mb: int | None = None,
    docker: dict | None = None,
    kubernetes: dict | None = None,
    nanoclaw: dict | None = None,
) -> dict:
    """Change some of a pool's options, keeping the rest (admin only). Pass the
    pool's `version` as expected_version; 409 stale_version means it changed: read
    it again. `env` merges (null removes a key). Name, target, runtime are fixed."""
    changes = {
        "image": image,
        "capabilities": capabilities,
        "desired_replicas": desired_replicas,
        "max_replicas": max_replicas,
        "agents_per_instance": agents_per_instance,
        "env": env,
        "cpus": cpus,
        "memory_mb": memory_mb,
        "docker": docker,
        "kubernetes": kubernetes,
        "nanoclaw": nanoclaw,
    }
    body = {k: v for k, v in changes.items() if v is not None}
    return await _call(
        ctx,
        "PATCH",
        f"/pools/{name}",
        json={"expected_version": expected_version, **body},
    )


@tool(MANAGERS)
async def get_instance(ctx: Context, instance_id: str) -> dict:
    """One instance: state, draining, image and digest, and its agents."""
    return await _call(ctx, "GET", f"/instances/{instance_id}")


@tool(MANAGERS)
async def get_instance_logs(
    ctx: Context, instance_id: str, tail: Annotated[int, Field(ge=1, le=500)] = 200
) -> dict:
    """The last `tail` lines (max 500) of an instance's container output, read live
    from its target."""
    logs = await _call(
        ctx, "GET", f"/instances/{instance_id}/logs", params={"tail": tail}
    )
    return {"logs": logs}


@tool(MANAGERS)
async def drain_agent(ctx: Context, agent_id: str) -> dict:
    """Let an agent finish its current task, then claim nothing new."""
    return await _call(ctx, "POST", f"/agents/{agent_id}/drain")


@tool(MANAGERS)
async def recycle_instance(
    ctx: Context, instance_id: str, requeue: bool = False
) -> dict:
    """Replace a running instance: its agents drain, it stops once none holds a
    claimed task, then a new one starts. By default work finishes first; with
    `requeue` its claimed tasks are released now (no attempt counted) so it stops
    on the next pass. Follow it with inspect_pool."""
    return await _call(
        ctx, "POST", f"/instances/{instance_id}/recycle", json={"requeue": requeue}
    )


@tool(MANAGERS)
async def scale_pool(ctx: Context, name: str, desired_replicas: int) -> dict:
    """Set how many instances a pool should run. Above the pool's cap, a human is
    asked to approve instead (the result carries a decision_id)."""
    return await _call(
        ctx, "POST", f"/pools/{name}/scale", json={"desired_replicas": desired_replicas}
    )


@tool(MANAGERS)
async def list_decisions(
    ctx: Context,
    state: Literal["open", "answered", "cancelled"] = "open",
    subject_id: str | None = None,
    project: str | None = None,
    kinds: list[DecisionKindArg] | None = None,
    view: ViewArg = "summary",
    limit: int = 25,
    cursor: str | None = None,
) -> dict:
    """Decisions (questions, approvals, reviews, policy, knowledge), oldest first, in
    pages. Summary rows have options but not the question; view=detail adds it."""
    return await _call(
        ctx,
        "GET",
        "/decisions",
        params={
            "state": state,
            "subject_id": subject_id,
            "project": project,
            "kind": kinds,
            "view": view,
            "limit": limit,
            "cursor": cursor,
        },
    )


@tool(MANAGERS)
async def get_decision(ctx: Context, decision_id: str) -> dict:
    """Read one decision in full: question, options, payload, answer."""
    return await _call(ctx, "GET", f"/decisions/{decision_id}")


@tool(MANAGERS)
async def answer_decision(
    ctx: Context,
    decision_id: str,
    choice: str | None = None,
    text: str = "",
    request_id: str | None = None,
) -> dict:
    """Answer an open decision addressed to you: one of its options as `choice`
    (e.g. approve/reject, accept/reject), and/or a text answer or notes. Pass a
    fresh `request_id` so a retry returns the stored answer (`replayed`), not 409."""
    return await _call(
        ctx,
        "POST",
        f"/decisions/{decision_id}/answer",
        json={"choice": choice, "text": text, "request_id": request_id},
    )


class Answer(BaseModel):
    decision_id: str
    choice: str | None = None
    text: str = ""
    request_id: str | None = None


@tool(MANAGERS)
async def answer_decisions(ctx: Context, answers: list[Answer]) -> dict:
    """Answer up to 20 decisions at once, each with its own explicit answer (as in
    answer_decision). Each is applied on its own: read `results[i].ok` and `error`
    per item; one failure does not stop the others."""
    return await _call(
        ctx,
        "POST",
        "/decisions/answers",
        json={"answers": [a.model_dump() for a in answers]},
    )


@tool(MANAGERS)
async def wait_for_events(
    ctx: Context,
    after: int | None = None,
    wait_s: Annotated[float, Field(ge=0, le=30)] = 20,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> dict:
    """Wait for what happens next instead of polling: events after cursor `after`
    (oldest first, ≤100), or up to wait_s seconds (max 30) for the first one.
    Without `after`, waits from now. Pass back `next_cursor`; `timed_out` means
    nothing happened. Filter by subject, e.g. ("instance", id) or ("task", id)."""
    return await _call(
        ctx,
        "GET",
        "/events/wait",
        params={
            "after": after,
            "wait_s": wait_s,
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
    )


@tool(MANAGERS)
async def get_fleet_capabilities(ctx: Context) -> dict:
    """What this node can actually run, measured now: each target's reachability,
    OCI runtimes and resources, and per pool whether a worker can be launched
    (`launchable`, `blockers`, per-check detail; ok=null means unverified)."""
    return await _call(ctx, "GET", "/fleet/capabilities")


@tool(MANAGERS)
async def launch_worker(
    ctx: Context, pool: str, expected_desired_replicas: int | None = None
) -> dict:
    """Request one more worker from a pool (adds 1 to its desired replicas, up to
    its cap). The result means *requested*, not running: poll list_pools or
    get_fleet_capabilities. Pass the pool's current desired_replicas as
    expected_desired_replicas so a retry cannot launch twice (409 stale_pool)."""
    return await _call(
        ctx,
        "POST",
        f"/pools/{pool}/launch",
        json={"expected_desired_replicas": expected_desired_replicas},
    )


@tool(MANAGERS)
async def list_worker_templates(ctx: Context) -> dict:
    """The worker templates this node supports, per node: `ready`, or `missing`
    (what to set up). Only a ready template can be created. Read-only. Defaults
    (suggested pool name, quantity 1, cap) are in each template's `defaults`."""
    return await _call(ctx, "GET", "/fleet/templates")


@tool(MANAGERS)
async def create_worker(
    ctx: Context,
    template: str,
    name: str,
    node: str = "local",
    quantity: int = 1,
    image: str | None = None,
    advanced: dict | None = None,
) -> dict:
    """Create a pool from a ready template and request `quantity` workers (default
    1). `name` is required and is the idempotency key: repeating a call is 409
    pool_exists, never a second worker (take a free name from `defaults.name` in
    list_worker_templates). Image, runtime, and options come from the template; `image` is only for
    argus-worker and must be one it offers. `advanced` (capabilities, env,
    extra_hosts, max_replicas) is admin only. The result means *requested*, not running: follow
    it with list_pools. A name that exists is 409 pool_exists (use launch_worker)."""
    return await _call(
        ctx,
        "POST",
        "/fleet/workers",
        json={
            "template": template,
            "node": node,
            "name": name,
            "quantity": quantity,
            "image": image,
            "advanced": advanced,
        },
    )


# -- preferred models -------------------------------------------------------------------


@tool(ANYONE)
async def get_model_policy(ctx: Context) -> dict:
    """The effective model policy: `harness` and `implementation` maps (engine -> ordered
    model chain), `revision`, `source` (env | file | defaults), and `env_overrides` (true
    means ARGUS_MODEL_POLICY wins over saved edits). IDs are opaque; availability at the
    provider is not checked."""
    return await _call(ctx, "GET", "/models")


@tool(ADMINS)
async def set_model_policy(ctx: Context, harness: dict, implementation: dict) -> dict:
    """Replace the whole model policy (admin only). Both maps are required; each maps an
    engine (codex, claude, opencode, agent) to a nonempty chain of at most 10 distinct
    model IDs. A bad chain is a 422 naming it. New NanoClaw instances pick it up."""
    return await _call(
        ctx,
        "PUT",
        "/models",
        json={"harness": harness, "implementation": implementation},
    )


@tool(ADMINS)
async def reset_model_policy(ctx: Context) -> dict:
    """Remove the saved model policy so the shipped defaults apply again (admin only)."""
    return await _call(ctx, "DELETE", "/models")


# -- engines and the local model ---------------------------------------------------------


@tool(ANYONE)
async def list_engines(ctx: Context) -> dict:
    """Engines (claude, codex, opencode, cursor-agent) and the local model. Each entry
    lists its `layers`: the NanoClaw `harness` and the code `executor` (claude and codex
    have both, cursor-agent and opencode only the executor). Every layer has its own
    `readiness` (installed, authenticated, validated; ok=null is unknown), `accounts` and
    `usable` (true only when all three hold). Validating one layer proves nothing about
    the other. The top-level `readiness`, `accounts`, `usable` and `role` are the
    deprecated single-layer view. Read-only."""
    return await _call(ctx, "GET", "/engines")


@tool(MANAGERS)
async def route_task(
    ctx: Context,
    summary: str,
    simple: bool = False,
    low_risk: bool = False,
    criteria_deterministic: bool = False,
    multi_file: bool = False,
    touches_sensitive: bool = False,
    is_code: bool = False,
    input_chars: int | None = None,
    output_tokens: int = 512,
    difficulty: Literal["trivial", "simple", "standard", "hard"] | None = None,
) -> dict:
    """Decide where a task should run: the local model, or codex/claude, and which code
    executor and model do the work. `difficulty` (the task's, when it has one) replaces
    `simple` (trivial and simple mean simple) and picks the executor chain: trivial and
    simple Cursor with composer-2.5, standard Claude or Codex, hard the strong model;
    an executor that is not usable is skipped, in chain order, with the reason.
    The local model is chosen only if the task is simple, low risk, has deterministic acceptance criteria,
    is not multi-file or sensitive, and `input_chars` (everything the model would see:
    instructions, tool schemas, history, inputs) provably fits. Unknown size never goes
    local. Needs the orchestrator or admin scope. The decision and its reason are recorded."""
    return await _call(
        ctx,
        "POST",
        "/engines/route",
        json={
            "summary": summary,
            "simple": simple,
            "low_risk": low_risk,
            "criteria_deterministic": criteria_deterministic,
            "multi_file": multi_file,
            "touches_sensitive": touches_sensitive,
            "is_code": is_code,
            "input_chars": input_chars,
            "output_tokens": output_tokens,
            "difficulty": difficulty,
        },
    )


@tool(ANYONE)
async def run_local(
    ctx: Context,
    prompt: str,
    system: str | None = None,
    criteria: dict | None = None,
    max_output_tokens: int = 512,
    max_attempts: int = 1,
) -> dict:
    """Run one bounded completion on the local model (no tools, no side effects) and
    check it against `criteria` ({json_schema, must_contain, equals, max_chars}).
    `verdict` is `accepted` (use `text`) or `escalate`: then do the work with
    `escalate_to` yourself. Cost is unknown (null), not zero. Use route_task first."""
    return await _call(
        ctx,
        "POST",
        "/engines/local/run",
        json={
            "prompt": prompt,
            "system": system,
            "criteria": criteria or {},
            "max_output_tokens": max_output_tokens,
            "max_attempts": max_attempts,
        },
    )


@tool(ANYONE)
async def get_project_runtime(ctx: Context, project: str) -> dict:
    """A project's declared runtime (approved components, test services), the gaps
    it records, and every derived image: state (preparing, building, verifying,
    ready, failed), the reason, probe evidence, the base image and the digest.
    `ready` always means every probe passed; read `failed` reasons, never infer."""
    return await _call(ctx, "GET", f"/projects/{project}/runtime")


@tool(ADMINS)
async def set_project_runtime(
    ctx: Context,
    project: str,
    components: list[dict],
    services: list[dict] | None = None,
    codex_turn_timeout_s: int | None = None,
) -> dict:
    """Declare what a project's tasks need in their image. `components` are
    {id, version?, required?} from GET /runtime/catalog (python, pango, graft, psql,
    postgres-server, valkey, node, git, chromium; pnpm, sops-age, minio, elasticsearch
    are recorded as gaps with required=false). `services` are disposable test services
    {kind: postgres|valkey, env: {VARIABLE: "{url}"}}. Never Dockerfile text: an
    unapproved declaration is refused with every problem (422 runtime_invalid)."""
    return await _call(
        ctx,
        "PUT",
        f"/projects/{project}/runtime",
        json={
            "components": components,
            "services": services or [],
            "codex_turn_timeout_s": codex_turn_timeout_s,
        },
    )


@tool(ADMINS)
async def rebuild_project_runtime(ctx: Context, project: str) -> dict:
    """Retry the failed derived-image builds of the project's current recipe and
    return its runtime-held tasks to ready. A changed declaration does this itself."""
    return await _call(ctx, "POST", f"/projects/{project}/runtime/rebuild")
