"""Built-in orchestrator parts below the loop: the in-process target and the
reconciler, adapters, usage, detection, notices and framing
(docs/specs/builtin-orchestrator.spec.md)."""

import asyncio
import json
import socket
from datetime import timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
from api_support import FakeClock
from argus_api import orchestrator
from argus_api.accounts import local_detect
from argus_api.conversations import service as conversations
from argus_api.db import Database
from argus_api.fleet import service as fleet
from argus_api.fleet.reconciler import FleetSettings, reconcile_pool
from argus_api.fleet.targets import (
    INPROCESS_LOG_LINES,
    InProcessHandle,
    InProcessTarget,
    TargetError,
    TargetRegistry,
)
from argus_api.orchestrator_builtin import prompt, tools
from argus_api.orchestrator_builtin.loop import model_http_client
from argus_api.orchestrator_builtin.models import (
    Endpoint,
    Failure,
    anthropic,
    openai_compat,
)
from argus_api.orchestrator_builtin.models import send as send_model
from argus_api.orchestrator_builtin.transcript import (
    Result,
    Step,
    ToolCall,
    Transcript,
)
from argus_api.settings import service as settings_service
from argus_api.tasks import service as tasks
from argus_core.conversations import ConversationCreate
from argus_core.tasks import TaskCreate
from fastapi import FastAPI

REPO = Path(__file__).resolve().parents[3]
FRAMING = json.loads(
    (REPO / "integrations/nanoclaw/fixtures/chat-framing/cases.json").read_text()
)
NO_URL = FleetSettings(argus_url=None, max_starts_in_flight=8)


async def test_model_http_client_routes_local_hosts_around_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("HTTPS_PROXY", "http://tls-proxy.invalid:9999")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    for name in ("ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)

    client = model_http_client(FastAPI())
    try:
        local_transports = [
            client._transport_for_url(httpx.URL(f"http://{host}/v1/chat/completions"))
            for host in ("127.0.0.1", "localhost", "[::1]", "host.docker.internal")
        ]
        assert all(transport is local_transports[0] for transport in local_transports)
        local_pool = local_transports[0]._pool
        assert getattr(local_pool, "_proxy_url", None) is None

        cloud_transport = client._transport_for_url(
            httpx.URL("https://api.openai.com/v1/chat/completions")
        )
        assert cloud_transport is not local_transports[0]
        cloud_proxy = cloud_transport._pool._proxy_url
        assert cloud_proxy.host == b"tls-proxy.invalid"
        assert client.follow_redirects is False
    finally:
        await client.aclose()


# -- the in-process target ---------------------------------------------------------


def _spec(instance_id: str = "i1"):
    from argus_api.fleet.targets import ContainerSpec

    return ContainerSpec(
        instance_id=instance_id,
        image="builtin",
        env={"ARGUS_POOL": "orchestrator"},
        labels={"argus.pool": "orchestrator"},
    )


class Runner:
    """A loop the test steers: it waits for `finish`, then returns or raises."""

    def __init__(self, *, ignore_stop: bool = False) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.fail = False
        self.ignore_stop = ignore_stop
        self.handles: list[InProcessHandle] = []

    async def __call__(self, handle: InProcessHandle) -> None:
        self.calls += 1
        self.handles.append(handle)
        self.started.set()
        waits = [asyncio.ensure_future(self.finish.wait())]
        if not self.ignore_stop:
            waits.append(asyncio.ensure_future(handle.stopping.wait()))
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for w in waits:
            w.cancel()
        if self.fail:
            raise ValueError("boom")


async def _settled(target: InProcessTarget) -> None:
    """Await every loop's task (the milestone), whatever it ended with."""
    if running := {loop.task for loop in target._loops.values()}:
        await asyncio.wait(running)


async def test_start_is_idempotent_and_list_reports_exits() -> None:
    runner = Runner()
    target = InProcessTarget(runner)
    ref = await target.start(_spec())
    assert await target.start(_spec()) == ref == "inprocess:i1"
    await runner.started.wait()
    assert runner.calls == 1
    [seen] = await target.list({"argus.pool": "orchestrator"})
    assert (seen.status, seen.exit_code) == ("running", None)
    assert await target.list({"argus.pool": "other"}) == []

    runner.finish.set()
    await _settled(target)
    [seen] = await target.list({})
    assert (seen.status, seen.exit_code) == ("exited", 0)

    crashing = Runner()
    crashing.fail = True
    other = InProcessTarget(crashing)
    await other.start(_spec("i2"))
    crashing.finish.set()
    await _settled(other)
    [seen] = await other.list({})
    assert (seen.status, seen.exit_code) == ("exited", 1)
    assert "crashed (ValueError: boom)" in await other.logs("inprocess:i2", tail=5)


async def test_stop_drains_then_cancels_after_the_grace() -> None:
    polite = Runner()
    target = InProcessTarget(polite)
    await target.start(_spec())
    await polite.started.wait()
    await target.stop("inprocess:i1", grace=timedelta(seconds=3600))
    assert polite.handles[0].stopping.is_set()
    assert await target.list({}) == []
    await target.stop("inprocess:unknown", grace=timedelta(0))  # a no-op

    stubborn = Runner(ignore_stop=True)
    target = InProcessTarget(stubborn)
    await target.start(_spec())
    await stubborn.started.wait()
    await target.stop("inprocess:i1", grace=timedelta(0))
    assert await target.list({}) == []


async def test_logs_are_bounded_and_pause_is_refused() -> None:
    target = InProcessTarget(Runner())
    await target.start(_spec())
    handle = target._loops["i1"].handle
    for n in range(INPROCESS_LOG_LINES + 20):
        handle.log(f"line {n}")
    lines = (await target.logs("inprocess:i1", tail=10_000)).splitlines()
    assert len(lines) == INPROCESS_LOG_LINES
    assert lines[-1] == f"line {INPROCESS_LOG_LINES + 19}"
    with pytest.raises(TargetError):
        await target.pause("inprocess:i1")
    assert (await target.probe()).version == "inprocess"
    await target.stop_all(timedelta(0))
    with pytest.raises(TargetError, match="stopping"):  # shutdown: no new loop
        await target.start(_spec("i2"))


async def _builtin_pool(db: Database) -> None:
    async with db.write() as tx:
        await fleet.upsert_pool(
            tx,
            orchestrator.SYSTEM_POOL,
            orchestrator._builtin_upsert(1),
            "admin:test",
            system=orchestrator.SYSTEM_MARKER,
        )


async def _live(db: Database):
    async with db.read() as conn:
        return await fleet.list_instances(conn, orchestrator.SYSTEM_POOL, False)


async def test_reconciler_starts_a_builtin_pool_without_a_public_url(
    db: Database,
) -> None:
    await _builtin_pool(db)
    runner = Runner()
    target = InProcessTarget(runner)
    registry = TargetRegistry(inprocess=target)
    await reconcile_pool(db, "orchestrator", registry, NO_URL)
    await runner.started.wait()
    [handle] = runner.handles
    assert set(handle.env) == {
        "ARGUS_URL",
        "ARGUS_BOOTSTRAP_TOKENS",
        "ARGUS_INSTANCE_ID",
        "ARGUS_POOL",
        "ARGUS_AGENT_ROLE",
    }
    assert (handle.env["ARGUS_URL"], handle.env["ARGUS_AGENT_ROLE"]) == (
        "inprocess",
        "orchestrator",
    )
    await reconcile_pool(db, "orchestrator", registry, NO_URL)
    [inst] = await _live(db)
    assert (inst.state, inst.provider_ref) == ("running", "inprocess:" + inst.id)
    await target.stop_all(timedelta(0))


async def test_a_crashed_loop_is_replaced_and_crash_loops_are_held(
    db: Database, clock: FakeClock
) -> None:
    await _builtin_pool(db)
    runner = Runner()
    runner.fail = True
    runner.finish.set()  # every loop crashes at once
    target = InProcessTarget(runner)
    registry = TargetRegistry(inprocess=target)

    async def crash_once() -> None:
        await reconcile_pool(db, "orchestrator", registry, NO_URL)  # start
        await _settled(target)
        clock.advance(3)  # died within seconds
        await reconcile_pool(db, "orchestrator", registry, NO_URL)  # confirm, replace

    for _ in range(3):
        await crash_once()
    assert runner.calls == 3
    await reconcile_pool(db, "orchestrator", registry, NO_URL)
    assert await _live(db) == []  # held after three quick crashes: nothing reserved
    async with db.read() as conn:
        pool = await fleet.get_pool(conn, "orchestrator")
    assert pool.status == "failing"
    await target.stop_all(timedelta(0))


# -- adapters and usage ------------------------------------------------------------


def _transcript() -> Transcript:
    t = Transcript()
    t.items.append(Step("Looking.", [ToolCall("call_1", "list_tasks", {}, "{}")]))
    t.items.append(Result("call_1", "list_tasks", '{"items":[]}', False, 0))
    t.items.append(Step("", [ToolCall("call_2", "get_task", None, "{bad")]))
    t.items.append(
        Result("call_2", "get_task", "arguments are not valid JSON", True, 1)
    )
    return t


def test_anthropic_body_caches_the_prefix_and_alternates_roles() -> None:
    tool = tools.ToolSpec("list_tasks", "List.", {"type": "object"})
    body = anthropic.AnthropicAdapter().body(
        "m", "SYS", [("user", "hi")], _transcript(), [tool], use_tools=False
    )
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["tool_choice"] == {"type": "none"}
    assert body["max_tokens"] == 4096 and "temperature" not in body
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert body["messages"][3]["content"][0]["input"] == {}  # unparsed arguments
    assert body["messages"][4]["content"][0]["is_error"] is True


def test_openai_body_marks_errors_and_keeps_raw_arguments() -> None:
    adapter = openai_compat.OpenAIAdapter(
        token_field="max_tokens", provider_block={"order": ["x"]}
    )
    body = adapter.body("m", "SYS", [("user", "hi")], _transcript(), [], use_tools=True)
    assert body["max_tokens"] == 4096 and body["provider"] == {"order": ["x"]}
    assert "tools" not in body
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][4]["tool_calls"][0]["function"]["arguments"] == "{bad"
    assert body["messages"][5]["content"].startswith("ERROR ")


def test_openai_body_returns_provider_tool_metadata() -> None:
    adapter = openai_compat.OpenAIAdapter(token_field="max_tokens")
    extra = {"google": {"thought_signature": "sig"}}
    reply = adapter.parse(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "extra_content": extra,
                                "function": {"name": "list_tasks", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ]
        }
    )
    assert reply.calls[0].extra_content == extra
    object_reply = adapter.parse(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "list_tasks", "arguments": {}},
                            }
                        ],
                    },
                }
            ]
        }
    )
    assert object_reply.calls[0].arguments == {}
    assert object_reply.calls[0].raw == "{}"
    transcript = Transcript()
    transcript.items.append(Step(reply.text, reply.calls))
    body = adapter.body("m", "SYS", [], transcript, [], use_tools=True)
    assert body["messages"][1]["tool_calls"][0]["extra_content"] == extra


def test_usage_is_normalised_so_input_never_counts_twice() -> None:
    a = anthropic.AnthropicAdapter().parse(
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 4,
                "cache_read_input_tokens": 90,
                "output_tokens": 7,
            },
        }
    )
    assert a.usage is not None
    assert (a.usage.input_tokens, a.usage.cached_input_tokens) == (14, 90)
    o = openai_compat.OpenAIAdapter(token_field="max_tokens").parse(
        {
            "choices": [{"message": {"content": None, "tool_calls": []}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 60},
            },
        }
    )
    assert o.usage is not None
    assert (o.usage.input_tokens, o.usage.cached_input_tokens) == (40, 60)
    assert o.text == ""
    bare = anthropic.AnthropicAdapter().parse({"content": []})
    assert bare.usage is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"choices": "bad"},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": "bad"}]},
        {"choices": [{"message": {"tool_calls": {}}}]},
        {"choices": [{"message": {"tool_calls": ["bad"]}}]},
        {"choices": [{"message": {"tool_calls": [{"function": "bad"}]}}]},
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "tool",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "[]"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        },
    ],
    ids=[
        "root-null",
        "root-list",
        "choices-not-list",
        "choices-empty",
        "choice-not-object",
        "message-not-object",
        "tool-calls-not-list",
        "call-not-object",
        "function-not-object",
        "id-missing",
        "id-empty",
        "type-invalid",
        "type-empty",
        "type-missing",
        "name-empty",
        "name-missing",
        "arguments-missing",
        "arguments-invalid-json",
        "arguments-not-object",
        "arguments-truncated",
        "finish-stop-with-call",
    ],
)
async def test_openai_send_classifies_malformed_protocol_as_invalid(payload) -> None:
    payload = json.loads(json.dumps(payload))
    if isinstance(payload, dict) and isinstance(payload.get("choices"), list):
        for choice in payload["choices"]:
            if (
                isinstance(choice, dict)
                and isinstance(choice.get("message"), dict)
                and "tool_calls" in choice["message"]
            ):
                choice.setdefault("finish_reason", "tool_calls")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as model_client:
        got = await send_model(
            model_client,
            Endpoint("http://model.test", {}, 5),
            openai_compat.OpenAIAdapter(token_field="max_tokens"),
            {},
        )

    assert isinstance(got, Failure)
    assert (got.kind, got.error_type) == ("invalid", "invalid_response")


async def test_openai_send_rejects_a_tool_call_without_a_finish_reason() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ]
                }
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as model_client:
        got = await send_model(
            model_client,
            Endpoint("http://model.test", {}, 5),
            openai_compat.OpenAIAdapter(token_field="max_tokens"),
            {},
        )

    assert isinstance(got, Failure)
    assert (got.kind, got.error_type) == ("invalid", "invalid_response")


async def test_openai_send_keeps_a_well_formed_tool_call() -> None:
    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"task_id":"t1"}',
                            },
                        }
                    ],
                },
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as model_client:
        got = await send_model(
            model_client,
            Endpoint("http://model.test", {}, 5),
            openai_compat.OpenAIAdapter(token_field="max_tokens"),
            {},
        )

    assert not isinstance(got, Failure)
    assert got.calls[0].arguments == {"task_id": "t1"}


async def test_send_sanitizes_an_attribute_error_from_a_malformed_adapter_payload() -> (
    None
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"content": ["malformed"]})
        )
    ) as model_client:
        got = await send_model(
            model_client,
            Endpoint("https://model.test", {}, 5),
            anthropic.AnthropicAdapter(),
            {},
        )

    assert isinstance(got, Failure)
    assert (got.kind, got.error_type) == ("invalid", "invalid_response")


@pytest.mark.parametrize(
    ("status", "body", "kind", "label"),
    [
        (429, {"error": {"type": "rate_limit_error"}}, "quota", "429 rate_limit_error"),
        (402, None, "quota", "402"),
        (
            401,
            {"error": {"type": "authentication_error"}},
            "auth",
            "401 authentication_error",
        ),
        (
            529,
            {"error": {"type": "overloaded_error"}},
            "transient",
            "529 overloaded_error",
        ),
        (409, None, "transient", "409"),
        (
            400,
            {"error": {"code": "context_length_exceeded"}},
            "too_long",
            "400 context_length_exceeded",
        ),
        (400, {"error": {"type": "invalid request: has spaces"}}, "request", "400"),
        (
            404,
            {"error": {"code": "model_not_found", "message": "secret text"}},
            "request",
            "404 model_not_found",
        ),
    ],
)
async def test_failures_are_classified_from_status_and_token_only(
    status: int, body, kind: str, label: str
) -> None:
    def answer(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers={"retry-after": "60"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        got = await send_model(
            client,
            Endpoint("https://model.test/v1", {}, 5),
            anthropic.AnthropicAdapter(),
            {},
        )
    assert isinstance(got, Failure)
    assert (got.kind, got.label) == (kind, label)
    assert "secret text" not in got.label
    if kind == "quota":
        assert got.reset is not None


def _raise(exc: BaseException, cause: BaseException | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if cause is None:
            raise exc
        raise exc from cause

    return handler


@pytest.mark.parametrize(
    ("handler", "kind", "label"),
    [
        # DNS cannot fix itself in 10 s: the endpoint is wrong for this runtime.
        (
            _raise(
                httpx.ConnectError("[Errno -2] Name or service not known"),
                socket.gaierror(-2, "Name or service not known"),
            ),
            "unreachable",
            "dns_failed",
        ),
        (
            _raise(
                httpx.ConnectError("All connection attempts failed"),
                ConnectionRefusedError(111, "Connect call failed"),
            ),
            "transient",
            "connection_refused",
        ),
        (_raise(httpx.ReadTimeout("timed out")), "transient", "timeout"),
        (_raise(httpx.ConnectError("closed")), "transient", "connect_failed"),
        (_raise(httpx.RemoteProtocolError("bad")), "invalid", "invalid_response"),
        (
            lambda _: httpx.Response(200, content=b"<html>not json</html>"),
            "invalid",
            "200 invalid_response",
        ),
        (
            lambda _: httpx.Response(307, headers={"location": "http://evil.test/"}),
            "unreachable",
            "307 redirect_refused",
        ),
    ],
)
async def test_transport_failures_are_classified_by_type_never_text(
    handler, kind: str, label: str
) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await send_model(
            client, Endpoint("https://m.test", {}, 5), anthropic.AnthropicAdapter(), {}
        )
    assert isinstance(got, Failure) and (got.kind, got.label) == (kind, label)
    assert "Name or service" not in got.label and "html" not in got.label


def test_results_are_cut_at_a_code_point_boundary() -> None:
    text = "é" * tools.RESULT_CAP  # two bytes each
    cut = tools.cap(text)
    shown, note = cut.rsplit("\n", 1)
    assert len(shown.encode()) <= tools.RESULT_CAP
    assert note == f"[cut: {len(shown.encode())} of {len(text.encode())} bytes]"
    assert tools.cap("short") == "short"


def test_elision_keeps_the_latest_step() -> None:
    t = _transcript()
    assert t.elide_earlier_steps()
    first, second = t.results()
    assert first.elided and first.text.startswith("[result elided: 12 bytes")
    assert not second.elided
    assert not t.elide_earlier_steps()
    assert t.elide_oldest() and second.elided and not t.elide_oldest()


# -- framing ---------------------------------------------------------------------


@pytest.mark.parametrize("case", FRAMING["inbound"], ids=lambda c: c["name"])
def test_inbound_framing_matches_the_channel(case: dict) -> None:
    claim = case["claim"]
    assert (
        prompt.render_inbound(claim["message"], claim["conversation"]) == case["text"]
    )


@pytest.mark.parametrize("case", FRAMING["titles"], ids=lambda c: c["reply"][:20])
def test_title_split_matches_the_channel(case: dict) -> None:
    assert prompt.split_title(case["reply"]) == (case["text"], case["title"])


def test_history_drops_duplicates_merges_roles_and_starts_with_the_user() -> None:
    conv = {"id": "c1", "kind": "chat", "title": "T", "title_source": "orchestrator"}

    def m(i: str, author: str, body: str, state: str = "answered") -> dict:
        return {"id": i, "author": author, "body": body, "state": state}

    claimed = m("m4", "admin:a", "now", "delivered")
    pairs = prompt.history(
        [
            m("m0", "orchestrator:x", "stale reply"),
            m("m1", "admin:a", "first"),
            m("m2", "system", "Task update.\nsource: origin"),
            m("m3", "orchestrator:x", "done"),
            claimed,
            m("m5", "admin:a", "next turn", "queued"),
        ],
        claimed,
        conv,
    )
    assert [role for role, _ in pairs] == ["user", "assistant", "user"]
    assert "first" in pairs[0][1] and "Server notice" in pairs[0][1]
    assert "TITLE:" not in pairs[0][1]  # only the claimed message asks for a title
    assert pairs[2][1].count("now") == 1 and "TITLE:" in pairs[2][1]
    assert all("next turn" not in text for _, text in pairs)


# -- local model detection -------------------------------------------------------


async def test_detection_finds_servers_and_follows_no_redirect() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        host, port = request.url.host, request.url.port
        if (host, port) == ("host.docker.internal", 11434):
            return httpx.Response(
                200, json={"data": [{"id": "qwen3:8b"}, {"id": "bad name!"}]}
            )
        if (host, port) == ("127.0.0.1", 1234):
            return httpx.Response(200, json={"data": [{"id": "llama-3"}]})
        if (host, port) == ("127.0.0.1", 11434):
            return httpx.Response(302, headers={"location": "http://evil.test/"})
        raise httpx.ConnectError("closed")

    found = await local_detect.detect(httpx.MockTransport(answer), in_container=True)
    by_url = {c.base_url: c for c in found.candidates}
    assert set(by_url) == {"http://host.docker.internal:11434", "http://127.0.0.1:1234"}
    ollama = by_url["http://host.docker.internal:11434"]
    assert (ollama.server_hint, ollama.models, ollama.preferred, ollama.note) == (
        "ollama",
        ["qwen3:8b"],
        True,
        None,
    )
    lmstudio = by_url["http://127.0.0.1:1234"]
    assert (lmstudio.preferred, lmstudio.note) == (False, local_detect.LOOPBACK_NOTE)
    assert found.hint is None  # something answered


@pytest.mark.parametrize("in_container", [False, True])
async def test_a_host_api_offers_the_worker_name_for_a_loopback_server(
    in_container: bool,
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if (request.url.host, request.url.port) == ("127.0.0.1", 8080):
            return httpx.Response(200, json={"data": [{"id": "spark-x2.6"}]})
        raise httpx.ConnectError("closed")  # host.docker.internal does not resolve

    found = await local_detect.detect(
        httpx.MockTransport(answer), in_container=in_container
    )
    by_url = {c.base_url: c for c in found.candidates}
    loopback = by_url.pop("http://127.0.0.1:8080")
    assert (loopback.api_endpoint, loopback.preferred) == (
        "http://127.0.0.1:8080",
        False,
    )
    if in_container:
        # Inside a container 127.0.0.1 is the container itself: no forwarder guess.
        assert by_url == {}
        return
    [forwarded] = by_url.values()
    assert (forwarded.base_url, forwarded.api_endpoint) == (
        "http://host.docker.internal:8080",
        "http://127.0.0.1:8080",
    )
    assert forwarded.preferred and forwarded.note == local_detect.FORWARDER_NOTE
    assert found.candidates[0] is forwarded  # preferred first


async def test_detection_hints_only_inside_a_container() -> None:
    def closed(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("closed")

    inside = await local_detect.detect(httpx.MockTransport(closed), in_container=True)
    assert inside.candidates == [] and inside.hint == local_detect.CONTAINER_HINT
    assert "0.0.0.0" not in inside.hint
    outside = await local_detect.detect(httpx.MockTransport(closed), in_container=False)
    assert outside.hint is None


# -- task update notices ---------------------------------------------------------


async def _conversation(db: Database, title: str = "chat") -> str:
    async with db.write() as tx:
        conv = await conversations.create_conversation(
            tx, ConversationCreate(title=title), "admin:t"
        )
    return conv.id


async def _origin_task(db: Database, conv_id: str) -> str:
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(title="t", project="demo"), "admin:t"
        )
        await tx.conn.execute(
            "UPDATE tasks SET origin_conversation_id = ? WHERE id = ?",
            (conv_id, task.id),
        )
    return task.id


async def _cancel(db: Database, task_id: str) -> None:
    async with db.write() as tx:
        await tasks.cancel(tx, task_id, "admin:t")


async def _notices(db: Database, conv_id: str) -> list[str]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT body FROM messages WHERE conversation_id = ? AND author = 'system'"
            " AND state = 'queued'",
            (conv_id,),
        ) as cur,
    ):
        return [r[0] for r in await cur.fetchall()]


async def _enable(db: Database, on: bool) -> None:
    async with db.write() as tx:
        await settings_service.put_setting(
            tx, Path(), settings_service.GLOBAL, "orchestrator.enabled", on, "admin:t"
        )


async def test_a_finished_task_queues_one_update_on_its_conversation(
    db: Database,
) -> None:
    await _enable(db, True)
    conv = await _conversation(db)
    first, second = await _origin_task(db, conv), await _origin_task(db, conv)
    await _cancel(db, first)
    await _cancel(db, second)  # covered by the notice still queued
    [body] = await _notices(db, conv)
    assert body == conversations.TASK_UPDATE_BODY
    assert first not in body and second not in body and "title" not in body.lower()


async def test_no_update_when_disabled_archived_or_full(db: Database) -> None:
    await _enable(db, False)
    off = await _conversation(db)
    await _cancel(db, await _origin_task(db, off))
    assert await _notices(db, off) == []

    await _enable(db, True)
    archived = await _conversation(db)
    task = await _origin_task(db, archived)
    async with db.write() as tx:
        await conversations.archive_conversation(tx, archived, "admin:t", archive=True)
    await _cancel(db, task)
    assert await _notices(db, archived) == []

    full = await _conversation(db)
    task = await _origin_task(db, full)
    async with db.write() as tx:
        for n in range(conversations.MAX_QUEUED):
            await tx.conn.execute(
                "INSERT INTO messages (id, conversation_id, author, body, state,"
                " truncated, created_at) VALUES (?, ?, 'admin:t', 'x', 'queued', 0, ?)",
                (f"q{n}", full, "2026-11-01T00:00:00+00:00"),
            )
    await _cancel(db, task)
    assert await _notices(db, full) == []


# -- the loop's concurrency (review fixes) ----------------------------------------


class FakeArgus:
    """An ArgusClient stand-in: each route answers from `routes`, optionally
    holding the call until the test releases it."""

    def __init__(self) -> None:
        from argus_api.orchestrator_builtin.client import Response

        self.Response = Response
        self.calls: list[str] = []
        self.hold: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}
        self.cancelled: list[str] = []
        self.answers: dict[str, tuple[int, object]] = {}
        self.token = "t"

    async def rest(self, method: str, path: str, **_: object):
        self.calls.append(path)
        self.entered.setdefault(path, asyncio.Event()).set()
        try:
            if path in self.hold:
                await self.hold[path].wait()
        except asyncio.CancelledError:
            self.cancelled.append(path)
            raise
        status, body = self.answers.get(path, (200, {}))
        return self.Response(status, body)


async def test_concurrent_acquires_take_one_lease() -> None:
    from argus_api.orchestrator_builtin.accounts import Engine

    argus = FakeArgus()
    argus.answers["/accounts/lease"] = (
        200,
        {"lease_id": "l1", "account_id": "a", "name": "a", "provider": "custom",
         "settings": {"model": "m"}},
    )  # fmt: skip
    argus.hold["/accounts/lease"] = asyncio.Event()
    engine = Engine(argus, lambda _: None)  # ty: ignore[invalid-argument-type]
    first = asyncio.create_task(engine.acquire())
    second = asyncio.create_task(engine.acquire())
    await argus.entered.setdefault("/accounts/lease", asyncio.Event()).wait()
    argus.hold["/accounts/lease"].set()
    assert await first and await second
    assert argus.calls.count("/accounts/lease") == 1


def _loop(argus: FakeArgus):
    from argus_api.orchestrator_builtin.loop import BuiltinLoop

    loop = BuiltinLoop(
        InProcessHandle(_spec()),
        argus,  # ty: ignore[invalid-argument-type]
        None,  # ty: ignore[invalid-argument-type]
    )
    loop.interval = 0.0
    return loop


async def test_the_heartbeat_runs_through_a_drain() -> None:
    argus = FakeArgus()
    argus.answers["/agents/me/heartbeat"] = (
        200,
        {"state": "draining", "heartbeat_interval_s": 0},
    )
    loop = _loop(argus)
    beats = asyncio.create_task(loop._heartbeat())
    await loop.stopping.wait()  # the drain stops claiming
    argus.entered.pop("/agents/me/heartbeat")
    again = asyncio.ensure_future(
        argus.entered.setdefault("/agents/me/heartbeat", asyncio.Event()).wait()
    )
    # Either another beat comes, or the heartbeat task ends: it must be the beat.
    await asyncio.wait({again, beats}, return_when=asyncio.FIRST_COMPLETED)
    assert again.done() and not beats.done()  # still proving liveness
    loop.done.set()
    await beats


async def test_a_cancelled_claim_cancels_its_poll() -> None:
    argus = FakeArgus()
    argus.hold["/agents/me/chat"] = asyncio.Event()  # a long poll that never ends
    loop = _loop(argus)
    claim = asyncio.create_task(loop.claim_once())
    await argus.entered.setdefault("/agents/me/chat", asyncio.Event()).wait()
    claim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await claim
    assert argus.cancelled == ["/agents/me/chat"]
