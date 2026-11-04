"""The built-in orchestrator in the running app: runtime resolution, turns against
a fake model API, failover, limits, and the loop's life
(docs/specs/builtin-orchestrator.spec.md)."""

import asyncio
import itertools
import json
import os
from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from argus_api import mcp_server, orchestrator
from argus_api.eventlog.routes import wait as wait_events
from argus_api.fleet.reconciler import reconcile_pool
from argus_api.fleet.targets import InProcessHandle, InProcessTarget, TargetRegistry
from argus_api.main import app
from argus_api.orchestrator_builtin import tools, turn
from argus_api.orchestrator_builtin.client import BASE_URL, ArgusClient
from argus_api.orchestrator_builtin.loop import BuiltinLoop
from argus_api.orchestrator_builtin.models import ANTHROPIC_URL, OPENAI_URL
from argus_api.secrets import MemorySecretStore
from argus_core.auth import Scope
from fastapi.testclient import TestClient
from test_launch import ADMIN

ADMIN_TOKEN = ADMIN["Authorization"].split()[1]
KEY_A = "sk-ant-api03-" + "a" * 40
KEY_B = "sk-ant-api03-" + "b" * 40
OPENAI_KEY = "sk-proj-" + "o" * 40
POLICY_CLAUDE = "claude-sonnet-5-5"  # first of the default harness chain for claude
# Every tool listed to the orchestrator scope that the built-in offers. A new tool
# fails `test_every_orchestrator_tool_is_classified` until it lands here or in
# `tools.HIDDEN` (spec 3.6).
OFFERED = frozenset(
    {
        "answer_decision", "answer_decisions", "attach_artifact", "cancel_steer",
        "cancel_task", "checkout_project_repository", "create_task",
        "create_task_draft", "discard_task_draft", "edit_task", "edit_task_draft",
        "get_agent_hygiene", "get_agent_models", "get_dead_session_recovery",
        "get_decision", "get_fleet_capabilities", "get_fleet_yield", "get_inbox",
        "get_instance", "get_instance_logs", "get_knowledge_page",
        "get_model_policy", "get_project_repository", "get_project_runtime",
        "get_routine", "get_settings", "get_task", "get_task_changes",
        "get_task_continuation", "get_task_delivery", "get_task_draft",
        "get_task_log", "get_task_models", "get_task_prerequisites", "get_tasks",
        "get_workspace_cleanup", "inspect_pool", "list_accounts", "list_agents",
        "list_artifacts", "list_decisions", "list_engines", "list_pools",
        "list_routines", "list_task_drafts", "list_task_inputs",
        "list_task_steers", "list_tasks", "list_worker_templates",
        "list_workspace_cleanup", "preview_decision", "publish_task_drafts",
        "read_artifact", "read_chat_attachment", "reconcile_task_delivery",
        "reconcile_task_pr", "recycle_instance", "refresh_account_quota",
        "release_task", "retry_task", "route_task", "run_local", "run_routine",
        "run_workspace_cleanup", "search_knowledge", "set_routine_enabled",
        "set_task_dependencies", "steer_task", "task_resumability_preflight",
        "unblock_task", "update_task_continuation", "upsert_routine",
        "verify_artifact", "wait_for_events", "write_knowledge",
    }
)  # fmt: skip


# -- a fake model API ------------------------------------------------------------

Answer = tuple[int, Any, dict[str, str]]


@dataclass
class Seen:
    url: str
    headers: httpx.Headers
    body: dict[str, Any]


class FakeModel:
    """Scripted answers, in order; an entry may be a function of the request."""

    def __init__(self) -> None:
        self.script: list[Answer | Callable[[dict[str, Any]], Answer]] = []
        self.requests: list[Seen] = []
        self._ids = itertools.count(1)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(Seen(str(request.url), request.headers, body))
        item = self.script.pop(0)
        status, payload, headers = item(body) if callable(item) else item
        return httpx.Response(status, json=payload, headers=headers)

    def text(self, text: str, *, tokens_in: int = 10, usage: bool = True) -> Answer:
        body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if usage:
            body["usage"] = {"input_tokens": tokens_in, "output_tokens": 5}
        return 200, body, {}

    def use(self, *calls: tuple[str, dict], tokens_in: int = 10) -> Answer:
        return (
            200,
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{next(self._ids)}",
                        "name": n,
                        "input": a,
                    }
                    for n, a in calls
                ],
                "usage": {"input_tokens": tokens_in, "output_tokens": 5},
            },
            {},
        )

    def oa_text(self, text: str) -> Answer:
        return 200, {"choices": [{"message": {"content": text}}]}, {}

    def oa_use(self, name: str, args: dict) -> Answer:
        call = {
            "id": f"call_{next(self._ids)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        return (
            200,
            {"choices": [{"message": {"content": None, "tool_calls": [call]}}]},
            {},
        )

    @staticmethod
    def error(status: int, kind: str | None = None, **headers: str) -> Answer:
        token = {"type": kind, "code": kind} if kind else {}
        return status, {"error": {**token, "message": "never read"}}, headers


@dataclass
class Env:
    client: TestClient
    model: FakeModel
    sleeps: list[float]
    loops: list[BuiltinLoop]
    targets: list[InProcessTarget]

    def call(self, fn, *args):
        return self.client.portal.call(fn, *args)  # ty: ignore[unresolved-attribute]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Env]:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    # As conftest sets them: claims must not depend on this host's free space.
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "false")
    monkeypatch.setenv("ARGUS_CLIENT_DOWNLOADS", "0")
    # Nothing but the test starts a loop: the reconciler's next pass is an hour away.
    monkeypatch.setenv("ARGUS_RECONCILE_INTERVAL_S", "3600")
    model, sleeps = FakeModel(), []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with TestClient(app) as client:
        app.state.secrets = MemorySecretStore()
        app.state.model_transport = httpx.MockTransport(model.handle)
        app.state.builtin_sleep = no_sleep
        found = Env(client, model, sleeps, [], [])
        yield found

        async def close() -> None:
            for loop in found.loops:
                await loop.argus.http.aclose()
                await loop.models.aclose()
            for target in found.targets:
                await target.stop_all(timedelta(0))

        found.call(close)
    del app.state.model_transport, app.state.builtin_sleep


def add_account(
    env: Env,
    name: str,
    *,
    provider: str = "anthropic",
    kind: str = "api_key",
    credential: dict | None = None,
    priority: int = 0,
    **extra: Any,
) -> str:
    res = env.client.post(
        "/api/accounts",
        json={
            "name": name,
            "provider": provider,
            "kind": kind,
            "priority": priority,
            "credential": credential or {"api_key": KEY_A},
            **extra,
        },
        headers=ADMIN,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def put_setting(env: Env, key: str, value: Any):
    return env.client.put(f"/api/settings/{key}", json={"value": value}, headers=ADMIN)


def enable(env: Env) -> None:
    res = put_setting(env, "orchestrator.enabled", True)
    assert res.status_code == 200, res.text


def system_pool(env: Env) -> dict:
    pools = env.client.get("/api/pools", headers=ADMIN).json()
    return next(p for p in pools if p["system"] == "orchestrator")


def chat(env: Env, text: str) -> str:
    conv = env.client.post("/api/conversations", json={}, headers=ADMIN).json()["id"]
    res = env.client.post(
        f"/api/conversations/{conv}/messages", json={"body": text}, headers=ADMIN
    )
    assert res.status_code == 200, res.text
    return conv


def messages(env: Env, conv: str) -> list[dict]:
    return env.client.get(f"/api/conversations/{conv}/messages", headers=ADMIN).json()[
        "items"
    ]


def boot(env: Env) -> BuiltinLoop:
    """Start the system pool's instance through the reconciler, then drive its
    loop by hand: register, read tools and skill, lease."""

    async def go() -> BuiltinLoop:
        handles: list[InProcessHandle] = []
        started = asyncio.Event()

        async def capture(handle: InProcessHandle) -> None:
            handles.append(handle)
            started.set()
            await handle.stopping.wait()

        target = InProcessTarget(capture)
        env.targets.append(target)
        db = app.state.db
        await reconcile_pool(
            db, "orchestrator", TargetRegistry(inprocess=target), app.state.fleet
        )
        await started.wait()
        loop = BuiltinLoop(
            handles[0],
            ArgusClient(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=BASE_URL
                )
            ),
            httpx.AsyncClient(transport=app.state.model_transport),
            sleep=app.state.builtin_sleep,
        )
        env.loops.append(loop)
        await loop.register()
        await loop.setup()
        assert await loop.engine.acquire()
        return loop

    return env.call(go)


def run_turn(env: Env, loop: BuiltinLoop) -> None:
    async def go() -> None:
        claim = await loop.claim_once()
        assert claim is not None, loop.handle.tail(20)
        await turn.run(loop.deps, claim)

    env.call(go)


def reply_of(env: Env, conv: str) -> str:
    last = messages(env, conv)[-1]
    assert last["author"].startswith("orchestrator:"), last
    return last["body"]


# -- runtime resolution ----------------------------------------------------------


def test_with_no_runtime_available_enabling_is_refused(env: Env) -> None:
    res = put_setting(env, "orchestrator.enabled", True)
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "orchestrator_unavailable"
    assert orchestrator.NO_BUILTIN_ACCOUNT in detail["details"]
    assert len(detail["details"]) > 1  # and the template's blockers
    put_setting(env, "orchestrator.runtime", "builtin")
    res = put_setting(env, "orchestrator.enabled", True)
    assert res.json()["detail"]["code"] == "builtin_no_account"


@pytest.mark.parametrize(
    "account",
    [
        {"provider": "anthropic", "credential": {"api_key": KEY_A}},
        {
            "provider": "openai",
            "credential": {"auth_mode": "apikey", "OPENAI_API_KEY": OPENAI_KEY},
        },
        {
            "provider": "custom",
            "credential": {"api_key": "sk-or-" + "c" * 40},
            "config": {
                "base_url": "https://openrouter.ai/api/v1",
                "label": "openrouter",
                "engine": {"model": "qwen/qwen3-coder"},
            },
        },
        {
            "provider": "local",
            "credential": {"base_url": "http://127.0.0.1:11434", "model": "qwen3"},
        },
    ],
    ids=lambda a: a["provider"],
)
def test_auto_resolves_to_builtin_with_a_capable_account(
    env: Env, account: dict
) -> None:
    add_account(env, "m", **account)
    onboarding = env.client.get("/api/onboarding", headers=ADMIN).json()
    assert (onboarding["orchestrator_runtime"], onboarding["builtin_account"]) == (
        "builtin",
        "m",
    )
    enable(env)
    pool = system_pool(env)
    assert (pool["runtime"], pool["target"], pool["image"]) == (
        "builtin",
        "inprocess",
        "builtin",
    )
    assert pool["desired_replicas"] == 1


def _nanoclaw_ready(env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install = tmp_path / "nanoclaw"
    install.mkdir()
    monkeypatch.setattr(
        app.state,
        "settings",
        replace(
            app.state.settings,
            nanoclaw_install_dir=str(install),
            nanoclaw_instances_root="/tmp/argus-nc-instances",
            nanoclaw_agent_image="nc-agent:1",
        ),
    )

    async def ready(conn, settings, targets) -> bool:
        return False  # ready, not sandboxed

    monkeypatch.setattr(orchestrator, "pick_sandboxed", ready)


def test_only_subscriptions_resolve_to_nanoclaw(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _nanoclaw_ready(env, monkeypatch, tmp_path)
    add_account(
        env, "sub", kind="subscription", credential={"oauth_token": "sk-ant-oat-x"}
    )
    enable(env)
    assert system_pool(env)["runtime"] == "nanoclaw"


def test_a_runtime_change_waits_for_the_orchestrator_to_stop(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _nanoclaw_ready(env, monkeypatch, tmp_path)
    add_account(env, "a")
    enable(env)
    assert system_pool(env)["runtime"] == "builtin"

    async def instance(state: str) -> None:
        async with app.state.db.write() as tx:
            await tx.conn.execute(
                "INSERT INTO instances (id, pool, provider_ref, state, created_at,"
                " terminated_at) VALUES ('i1', 'orchestrator', 'inprocess:i1', ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET state = excluded.state,"
                " terminated_at = excluded.terminated_at",
                (
                    state,
                    "2026-11-01T00:00:00+00:00",
                    "2026-11-01T00:01:00+00:00" if state == "terminated" else None,
                ),
            )

    env.call(instance, "running")
    res = put_setting(env, "orchestrator.runtime", "nanoclaw")
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "orchestrator_running"
    assert system_pool(env)["runtime"] == "builtin"

    env.call(instance, "terminated")
    res = put_setting(env, "orchestrator.runtime", "nanoclaw")
    assert res.status_code == 200, res.text
    pool = system_pool(env)
    assert (pool["runtime"], pool["target"]) == ("nanoclaw", "docker")


def test_only_enabling_writes_an_inprocess_pool(env: Env) -> None:
    res = env.client.put(
        "/api/pools/mine",
        json={
            "image": "builtin",
            "target": "inprocess",
            "runtime": "builtin",
            "max_replicas": 1,
        },
        headers=ADMIN,
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "inprocess_system_only"

    add_account(env, "a")
    enable(env)
    pool = system_pool(env)
    res = env.client.patch(
        "/api/pools/orchestrator",
        json={"expected_version": pool["version"], "image": "other"},
        headers=ADMIN,
    )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "system_pool"


# -- tools -----------------------------------------------------------------------


def test_every_orchestrator_tool_is_classified() -> None:
    listed = {n for n, a in mcp_server.audiences.items() if Scope.ORCHESTRATOR in a}
    assert listed == OFFERED | tools.HIDDEN
    assert not OFFERED & tools.HIDDEN
    assert set(tools.CORE) <= OFFERED


def test_hidden_tools_are_not_offered_and_routes_still_refuse(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    assert loop.deps is not None
    offered = {t.name for t in loop.deps.tools}
    assert offered == OFFERED
    result = env.call(
        loop.argus.mcp,
        "tools/call",
        {"name": "launch_worker", "arguments": {"pool": "web", "unconditional": True}},
    )
    assert result["isError"]
    assert "403 internal_orchestrator" in result["content"][0]["text"]


def test_a_small_local_window_gets_the_core_profile(env: Env) -> None:
    add_account(
        env,
        "local",
        provider="local",
        credential={
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen3",
            "context_tokens": 32768,
        },
    )
    enable(env)
    loop = boot(env)
    conv = chat(env, "hello")
    env.model.script = [(200, {"choices": [{"message": {"content": "Hi."}}]}, {})]
    run_turn(env, loop)
    [request] = env.model.requests
    assert request.url == "http://127.0.0.1:11434/v1/chat/completions"
    assert {t["function"]["name"] for t in request.body["tools"]} == set(tools.CORE)
    assert "Only a core set of tools" in request.body["messages"][0]["content"]
    assert reply_of(env, conv) == "Hi."


# -- turns -----------------------------------------------------------------------


def _usage_rows(env: Env, conv: str) -> list[dict]:
    async def read() -> list[dict]:
        async with (
            app.state.db.read() as conn,
            conn.execute(
                "SELECT * FROM conversation_usage WHERE conversation_id = ? ORDER BY id",
                (conv,),
            ) as cur,
        ):
            return [dict(r) for r in await cur.fetchall()]

    return env.call(read)


def test_a_turn_end_to_end(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    conv = chat(env, "What tasks are open?")
    model = env.model
    model.script = [
        model.use(("list_tasks", {})),
        model.text("TITLE: Open tasks\nNone are open."),
    ]
    run_turn(env, loop)

    assert reply_of(env, conv) == "None are open."
    title = env.client.get(f"/api/conversations/{conv}", headers=ADMIN).json()
    assert title["title"] == "Open tasks"
    first, second = model.requests
    assert first.url == ANTHROPIC_URL and first.headers["x-api-key"] == KEY_A
    assert first.body["model"] == POLICY_CLAUDE
    assert (
        "Trusted operator instruction"
        in first.body["messages"][0]["content"][0]["text"]
    )
    assert "# The argus-orchestrator skill" in first.body["system"][0]["text"]
    result = second.body["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"] is False
    assert '"items"' in result["content"]
    rows = _usage_rows(env, conv)
    assert [(r["input_tokens"], r["model"], r["measurement"]) for r in rows] == [
        (10, POLICY_CLAUDE, "measured"),
        (10, POLICY_CLAUDE, "measured"),
    ]
    log = loop.handle.tail(50)
    assert any(line.startswith("step 1: list_tasks") for line in log)
    assert any("finished (1 steps, 20/10 tokens)" in line for line in log)


def test_tasks_from_two_turns_in_flight_keep_their_origin(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    first, second = chat(env, "Make one"), chat(env, "Make another")

    def answer(body: dict) -> Answer:
        conv = body["system"][0]["text"].split("Id: ", 1)[1].split(".", 1)[0]
        last = body["messages"][-1]["content"][0]
        if last.get("type") == "tool_result":
            return env.model.text("Created.")
        return env.model.use(
            ("create_task", {"title": f"for {conv}", "project": "demo"})
        )

    env.model.script = [answer] * 4

    async def both() -> None:
        claims = [await loop.claim_once(), await loop.claim_once()]
        assert all(claims)
        await asyncio.gather(*(turn.run(loop.deps, c) for c in claims))

    env.call(both)

    async def origins() -> dict[str, str]:
        async with (
            app.state.db.read() as conn,
            conn.execute("SELECT title, origin_conversation_id FROM tasks") as cur,
        ):
            return {r[0]: r[1] for r in await cur.fetchall()}

    assert env.call(origins) == {f"for {first}": first, f"for {second}": second}


def test_a_quota_moves_the_same_step_to_the_next_account(env: Env) -> None:
    a = add_account(env, "a", priority=5)
    add_account(env, "b", credential={"api_key": KEY_B}, priority=1)
    enable(env)
    loop = boot(env)
    conv = chat(env, "hi")
    model = env.model
    model.script = [
        model.error(429, "rate_limit_error", **{"retry-after": "3600"}),
        model.text("Done."),
    ]
    run_turn(env, loop)
    assert (
        reply_of(env, conv) == "Switched to account `b` after a provider limit.\nDone."
    )
    first, second = model.requests
    assert first.headers["x-api-key"] == KEY_A and second.headers["x-api-key"] == KEY_B
    assert first.body["messages"] == second.body["messages"]  # the same step
    accounts = env.client.get("/api/accounts", headers=ADMIN).json()
    assert next(x for x in accounts if x["id"] == a)["state"] == "exhausted"
    assert "failover from a to b" in loop.handle.tail(20)


def test_a_quota_with_no_account_left_replies_with_the_reset(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    conv = chat(env, "hi")
    env.model.script = [env.model.error(429, None, **{"retry-after": "3600"})]
    run_turn(env, loop)
    assert "The earliest reset is 20" in reply_of(env, conv)
    assert loop.engine.lease is None


def test_a_rejected_key_is_recorded_and_named(env: Env) -> None:
    a = add_account(env, "a")
    enable(env)
    loop = boot(env)
    conv = chat(env, "hi")
    env.model.script = [env.model.error(401, "authentication_error")]
    run_turn(env, loop)
    assert reply_of(env, conv) == (
        "The model account `a` was rejected. Sign it in again or replace its key."
    )
    assert loop.engine.lease is None

    async def kinds() -> list[str]:
        async with (
            app.state.db.read() as conn,
            conn.execute(
                "SELECT kind FROM events WHERE subject_type = 'account'"
                " AND subject_id = ?",
                (a,),
            ) as cur,
        ):
            return [r[0] for r in await cur.fetchall()]

    assert "account.auth_rejected" in env.call(kinds)
    accounts = env.client.get("/api/accounts", headers=ADMIN).json()
    assert next(x for x in accounts if x["id"] == a)["state"] == "available"


def test_transient_errors_are_retried_twice_then_replied(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    conv = chat(env, "hi")
    env.model.script = [env.model.error(503)] * 3
    run_turn(env, loop)
    assert reply_of(env, conv) == (
        "The model provider did not answer (`503`). Send the message again."
    )
    assert env.sleeps == [2.0, 8.0]


def test_a_too_long_request_elides_earlier_results_once(env: Env) -> None:
    add_account(
        env,
        "oa",
        provider="openai",
        credential={"auth_mode": "apikey", "OPENAI_API_KEY": OPENAI_KEY},
    )
    enable(env)
    loop = boot(env)
    conv = chat(env, "hi")
    model = env.model
    model.script = [
        model.oa_use("list_tasks", {}),
        model.oa_use("get_settings", {}),
        model.error(400, "context_length_exceeded"),
        model.oa_text("Fits now."),
    ]
    run_turn(env, loop)
    assert reply_of(env, conv) == "Fits now."
    first, *_, retry = model.requests
    assert first.url == OPENAI_URL and first.body["max_completion_tokens"] == 4096
    results = [m["content"] for m in retry.body["messages"] if m["role"] == "tool"]
    assert results[0].startswith("[result elided:")
    assert not results[1].startswith("[result elided:")


def test_the_step_limit_ends_with_one_call_without_tools(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turn, "STEP_LIMIT", 2)
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    conv = chat(env, "loop forever")
    model = env.model
    model.script = [
        model.use(("list_tasks", {})),
        model.use(("list_tasks", {})),
        model.text("Summary."),
    ]
    run_turn(env, loop)
    assert reply_of(env, conv) == "Summary."
    final = model.requests[-1].body
    assert final["tool_choice"] == {"type": "none"}
    assert final["messages"][-1]["content"][-1]["text"] == turn.STEP_LIMIT_TEXT


def test_the_cap_mid_turn_stops_without_another_call(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    assert put_setting(env, "orchestrator.daily_budget_usd", 0.01).status_code == 200
    loop = boot(env)
    conv = chat(env, "spend")
    model = env.model
    model.script = [
        model.use(("list_tasks", {})),
        model.use(("list_tasks", {}), tokens_in=1_000_000),
    ]
    run_turn(env, loop)
    assert reply_of(env, conv) == (
        "Daily budget reached during this turn. It stopped after 1 tool calls:"
        " `list_tasks` (ok)."
    )
    assert len(model.requests) == 2


def test_the_key_is_redacted_and_missing_usage_is_unknown(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    conv = chat(env, "hi")
    env.model.script = [env.model.text(f"Your key is {KEY_A}.", usage=False)]
    run_turn(env, loop)
    assert reply_of(env, conv) == "Your key is [redacted]."
    [row] = _usage_rows(env, conv)
    assert (row["input_tokens"], row["measurement"]) == (None, "unknown")


def test_the_usage_route_needs_a_live_turn(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    loop = boot(env)
    conv = chat(env, "hi")
    res = env.call(
        loop.argus.rest,
        "POST",
        f"/agents/me/chat/{conv}/usage",
    )
    assert res.status == 422
    body = {"turn_id": "nope", "usage": {"input_tokens": 1, "model": POLICY_CLAUDE}}

    async def post() -> Any:
        return await loop.argus.rest("POST", f"/agents/me/chat/{conv}/usage", json=body)

    res = env.call(post)
    assert (res.status, res.code) == (409, "turn_not_open")


# -- the loop's life -------------------------------------------------------------


def test_the_loop_claims_replies_and_leaves_on_stop(env: Env) -> None:
    add_account(env, "a")
    enable(env)
    conv = chat(env, "hello")
    env.model.script = [env.model.text("Hello back.")]

    async def run() -> list[str]:
        db = app.state.db
        cursor = (await wait_events(db, None, 0)).next_cursor
        await reconcile_pool(db, "orchestrator", app.state.targets, app.state.fleet)
        while True:
            got = await wait_events(db, cursor, 30, "conversation", conv)
            cursor = got.next_cursor
            if any(
                e.kind == "chat.turn" and e.data.get("phase") == "finished"
                for e in got.events
            ):
                break
        await app.state.inprocess.stop_all(timedelta(seconds=30))
        async with db.read() as conn, conn.execute("SELECT state FROM agents") as cur:
            return [r[0] for r in await cur.fetchall()]

    assert env.call(run) == ["gone"]
    assert reply_of(env, conv) == "Hello back."
