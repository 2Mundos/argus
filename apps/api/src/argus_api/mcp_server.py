"""MCP endpoint for agents (worker lifecycle 3.7), served at /mcp/.

Every tool is a thin call to the REST API, made in-process with the agent's own
bearer token. Scopes, validation, leases, and errors therefore behave exactly
as over HTTP; this module holds no state or logic of its own.

Connect with `Authorization: Bearer <agent token>` (or an admin token for the
orchestrator-style tools).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx2 as httpx
from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

INSTRUCTIONS = """\
Argus coordinates a fleet of agents. As a worker: claim_task, then work on it while
calling heartbeat (renews your leases) and report_progress; finish with complete_task
or fail_task. Every task write needs the task's `attempt`; a 409 error means you no
longer hold the task, so stop working on it. If you need a human or orchestrator to
decide something, use ask_question (your task is paused until answered). Read the
knowledge pages listed with each claimed task before starting; search_knowledge for
more, and write_knowledge when you learn something others will need. If a provider
account runs out of quota, call report_account_exhausted to get the next one."""

server = MCPServer("argus", version="0.1.0", instructions=INSTRUCTIONS)

_app: FastAPI | None = None


def bind(app: FastAPI) -> None:
    """Point the tools at the REST app they call into (set once at import)."""
    global _app
    _app = app


class SlashAlias:
    """Serve `/mcp` as well as `/mcp/`: some MCP clients do not follow a
    redirect on POST."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            raise RuntimeError("MCP endpoint used outside the app lifespan")
        await self._app(scope, receive, send)


endpoint = Endpoint()


def _build() -> ASGIApp:
    # DNS-rebinding protection only allows localhost Host headers, but agents in
    # containers reach Argus by other names. Every tool call needs a bearer token
    # a browser page cannot attach, which is what that protection guards against.
    return server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
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
    auth = (ctx.headers or {}).get("authorization")
    if not auth:
        raise ToolError(
            "missing token: connect with the header 'Authorization: Bearer <token>'"
        )
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
    body = res.json() if res.content else None
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


# -- working on tasks ---------------------------------------------------------------


@server.tool()
async def claim_task(
    ctx: Context, capabilities: list[str] | None = None, wait_s: float = 20
) -> dict:
    """Claim the next task you can do, waiting up to wait_s seconds (max 30) for one.
    Returns the task (keep its `id` and `attempt`) with `knowledge_refs`: pages to
    read first. Returns {"task": null} when nothing became ready."""
    task = await _call(
        ctx,
        "POST",
        "/tasks/claim",
        json={"capabilities": capabilities or [], "wait_s": wait_s},
    )
    return {"task": task}


@server.tool()
async def heartbeat(ctx: Context, leases: list[Lease] | None = None) -> dict:
    """Prove you are alive and renew the leases on tasks you hold. Call it at least
    every heartbeat_interval_s (30 s). If a lease comes back rejected, stop working on
    that task. If state is "draining", finish your task and then call leave."""
    return await _call(
        ctx,
        "POST",
        "/agents/me/heartbeat",
        json={"leases": [lease.model_dump() for lease in leases or []]},
    )


@server.tool()
async def get_task(ctx: Context, task_id: str) -> dict:
    """Read a task: spec, acceptance criteria, state, attempt, clarifications."""
    return await _call(ctx, "GET", f"/tasks/{task_id}")


@server.tool()
async def report_progress(
    ctx: Context, task_id: str, attempt: int, message: str, percent: int | None = None
) -> dict:
    """Post a short progress note shown to humans (at most one per 10 s). It also
    renews your lease on the task."""
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/progress",
        json={"attempt": attempt, "message": message, "percent": percent},
    )


@server.tool()
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
) -> dict:
    """Finish the task. `summary` says what you did (Markdown); `data` must match the
    task kind's result schema if it has one; `artifacts` are ids from
    upload_text_artifact. Report token usage and the account you used, if known."""
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
    }
    if any(v is not None for v in usage.values()):
        result["usage"] = usage
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/complete",
        json={"attempt": attempt, "result": result},
    )


@server.tool()
async def fail_task(ctx: Context, task_id: str, attempt: int, error: str) -> dict:
    """Give up on the task, explaining why. It is retried (by you or another agent)
    until it runs out of attempts."""
    return await _call(
        ctx, "POST", f"/tasks/{task_id}/fail", json={"attempt": attempt, "error": error}
    )


@server.tool()
async def ask_question(
    ctx: Context,
    task_id: str,
    attempt: int,
    question: str,
    options: list[str] | None = None,
    audience: Literal["any", "orchestrator", "human"] = "any",
) -> dict:
    """Ask for a decision you cannot make yourself. Your task is paused (its lease is
    released) until someone answers; the answer is added to the task spec under
    "Clarifications" and the task becomes claimable again."""
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/ask",
        json={
            "attempt": attempt,
            "question": question,
            "options": options,
            "audience": audience,
        },
    )


@server.tool()
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
) -> dict:
    """Create a task. Workers may only create subtasks of a task they hold (set
    parent_id; the project is inherited). Orchestrators create any task and must name
    a project. `knowledge` pins pages the claiming agent should read."""
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
    return await _call(ctx, "POST", "/tasks", json=body)


@server.tool()
async def upload_text_artifact(
    ctx: Context,
    task_id: str,
    attempt: int,
    name: str,
    content: str,
    media_type: str = "text/plain",
) -> dict:
    """Attach a text file (report, diff, log, notes) to the task you hold. Pass the
    returned id in complete_task's `artifacts`. Names: letters, digits, . _ -"""
    return await _call(
        ctx,
        "POST",
        f"/tasks/{task_id}/artifacts",
        params={"attempt": attempt, "name": name},
        content=content.encode(),
        headers={"Content-Type": media_type},
    )


@server.tool()
async def leave(ctx: Context) -> dict:
    """End your identity cleanly (when drained or shutting down). Any task you still
    hold is released for someone else."""
    return await _call(ctx, "POST", "/agents/me/leave")


# -- knowledge ---------------------------------------------------------------------------


@server.tool()
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


@server.tool()
async def get_knowledge_page(
    ctx: Context, page_id: str, revision: int | None = None
) -> dict:
    """Read a knowledge page (Markdown body). Keep `revision_id` if you plan to edit it."""
    return await _call(
        ctx, "GET", f"/knowledge/pages/{page_id}", params={"revision": revision}
    )


@server.tool()
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


@server.tool()
async def lease_account(ctx: Context, providers: list[str]) -> dict:
    """Get a provider account (with its credential) to do your work, from the
    providers you can use, most preferred first (e.g. ["anthropic", "openai"])."""
    return await _call(ctx, "POST", "/accounts/lease", json={"providers": providers})


@server.tool()
async def report_account_exhausted(
    ctx: Context,
    account_id: str,
    reason: str = "",
    until: str | None = None,
    task_id: str | None = None,
    attempt: int | None = None,
) -> dict:
    """Your account hit a quota or rate limit. Returns the next account to use, or
    none: then pass task_id/attempt so the task waits until an account frees up.
    `until` is the provider's reset time (ISO 8601) if it told you."""
    return await _call(
        ctx,
        "POST",
        f"/accounts/{account_id}/exhausted",
        json={"reason": reason, "until": until, "task_id": task_id, "attempt": attempt},
    )


# -- orchestration ---------------------------------------------------------------------------


@server.tool()
async def list_tasks(
    ctx: Context, state: str | None = None, limit: int = 50
) -> list[dict]:
    """List tasks, newest first, optionally by state (ready, claimed, blocked,
    in_review, succeeded, failed, …)."""
    return await _call(ctx, "GET", "/tasks", params={"state": state, "limit": limit})


@server.tool()
async def cancel_task(ctx: Context, task_id: str) -> dict:
    """Cancel a task (orchestrators and admins)."""
    return await _call(ctx, "POST", f"/tasks/{task_id}/cancel")


@server.tool()
async def list_agents(ctx: Context, state: str | None = None) -> list[dict]:
    """List agents (active, draining, gone, lost) and how many tasks each holds."""
    return await _call(ctx, "GET", "/agents", params={"state": state})


@server.tool()
async def list_pools(ctx: Context) -> list[dict]:
    """List worker pools with desired/max replicas and live instances."""
    return await _call(ctx, "GET", "/pools")


@server.tool()
async def scale_pool(ctx: Context, name: str, desired_replicas: int) -> dict:
    """Set how many instances a pool should run. Above the pool's cap, a human is
    asked to approve instead (the result carries a decision_id)."""
    return await _call(
        ctx, "POST", f"/pools/{name}/scale", json={"desired_replicas": desired_replicas}
    )


@server.tool()
async def list_decisions(ctx: Context) -> list[dict]:
    """Open decisions (questions, approvals, reviews, proposals), oldest first."""
    return await _call(ctx, "GET", "/decisions")


@server.tool()
async def answer_decision(
    ctx: Context, decision_id: str, choice: str | None = None, text: str = ""
) -> dict:
    """Answer an open decision addressed to you: one of its options as `choice`
    (e.g. approve/reject, accept/reject), and/or a text answer or notes."""
    return await _call(
        ctx,
        "POST",
        f"/decisions/{decision_id}/answer",
        json={"choice": choice, "text": text},
    )
