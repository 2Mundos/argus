"""Engines, executors and the local model (docs/specs/engines.spec.md)."""

import json
import os
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import httpx2 as httpx
import pytest
from argus_api.engines import catalog, router
from argus_api.engines.local import (
    LocalModel,
    build_chat_completion_payload,
    check,
    validate_url,
)
from argus_api.engines.local_readiness import local_identity
from argus_api.main import app
from argus_core.criteria_safe import assert_schema_definition
from argus_core.engines import (
    Criteria,
    Fact,
    LocalModelInfo,
    LocalRunRequest,
    RouteRequest,
)
from fastapi.testclient import TestClient
from test_launch import ADMIN, agent_token, bearer
from test_mcp import tool

MODEL = "qwen-test"
NOW = datetime(2026, 1, 1, tzinfo=UTC).isoformat()


def ok_fact(detail: str = "checked") -> dict:
    return {"ok": True, "detail": detail, "at": NOW}


def full_ledger(*ids: str) -> dict:
    return {
        i: {k: ok_fact() for k in ("installed", "authenticated", "validated")}
        for i in ids
    }


class FakeServer:
    """A llama.cpp-style server: models, props, chat completions (scripted)."""

    def __init__(self, n_ctx: int | None = 65536) -> None:
        self.n_ctx = n_ctx
        self.chat: list[dict] = []
        self.answers: list[str | Exception] = ["pong"]
        self.finish = "stop"
        self.served = MODEL

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            meta = {"n_ctx": self.n_ctx} if self.n_ctx else {}
            return httpx.Response(
                200, json={"data": [{"id": self.served, "meta": meta}]}
            )
        if path == "/props":
            return httpx.Response(
                200,
                json={
                    "total_slots": 1,
                    "default_generation_settings": {"n_ctx": self.n_ctx},
                },
            )
        assert path == "/v1/chat/completions", path
        body = json.loads(request.content)
        self.chat.append(body)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": answer},
                        "finish_reason": self.finish,
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    def model(self) -> LocalModel:
        return LocalModel(
            "http://local.test:8080",
            MODEL,
            transport=httpx.MockTransport(self.handler),
        )


# -- criteria ---------------------------------------------------------------------------


def test_check_reports_every_failed_criterion() -> None:
    c = Criteria(must_contain=["a", "zz"], equals="abc", max_chars=2)
    failures = check("abcde", c)
    assert any("longer than 2" in f for f in failures)
    assert "missing 'zz'" in failures
    assert any("does not equal" in f for f in failures)
    assert check("", Criteria(must_contain=["x"])) == ["empty answer"]
    assert check("ok", Criteria(must_contain=["ok"])) == []


def test_must_match_in_request_is_rejected_with_422(client: TestClient) -> None:
    res = client.post(
        "/api/engines/local/run",
        json={
            "prompt": "x",
            "criteria": {"must_contain": ["x"], "must_match": ".*"},
        },
        headers=ADMIN,
    )
    assert res.status_code == 422


def test_unsafe_json_schema_rejected_before_model(client: TestClient) -> None:
    res = client.post(
        "/api/engines/local/run",
        json={
            "prompt": "x",
            "criteria": {
                "must_contain": ["x"],
                "json_schema": {"$ref": "http://evil/schema"},
            },
        },
        headers=ADMIN,
    )
    assert res.status_code == 422
    assert (
        client.post(
            "/api/engines/local/run",
            json={"prompt": "x", "criteria": {"pattern": ".*"}},
            headers=ADMIN,
        ).status_code
        == 422
    )


def test_empty_criteria_rejected_with_422(client: TestClient) -> None:
    assert (
        client.post(
            "/api/engines/local/run", json={"prompt": "x"}, headers=ADMIN
        ).status_code
        == 422
    )


def test_check_json_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"label": {"type": "string", "enum": ["pos", "neg"]}},
        "required": ["label"],
    }
    c = Criteria(json_schema=schema)
    assert check('{"label": "pos"}', c) == []
    assert check('{"label": "meh"}', c)
    assert check("not json", c)


def test_url_rules() -> None:
    assert validate_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    for bad in ("ftp://x", "http://user:pw@x", "http://x/?k=1", "nonsense"):
        with pytest.raises(ValueError):
            validate_url(bad)


# -- the local client -------------------------------------------------------------------


async def test_probe_measures_context_and_slots() -> None:
    info = await FakeServer().model().probe()
    assert (info.reachable, info.model, info.n_ctx, info.slots) == (
        True,
        MODEL,
        65536,
        1,
    )


async def test_probe_reports_a_model_that_is_not_the_allowed_one() -> None:
    server = FakeServer()
    server.served = "something-else"
    info = await server.model().probe()
    assert info.reachable and "not served" in (info.error or "")


async def test_probe_unreachable() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    local = LocalModel(
        "http://local.test", MODEL, transport=httpx.MockTransport(refuse)
    )
    info = await local.probe()
    assert not info.reachable and "refused" in (info.error or "")


async def test_accepted_run_sends_the_allowed_model_and_budget() -> None:
    server = FakeServer()
    result = await server.model().run(
        LocalRunRequest(prompt="say pong", criteria=Criteria(must_contain=["pong"])),
        escalate_to="codex",
    )
    assert result.verdict == "accepted" and result.text == "pong"
    assert result.escalate_to is None and result.cost is None
    sent = server.chat[0]
    assert sent["model"] == MODEL and sent["max_tokens"] == 512
    assert sent["temperature"] == 0 and "tools" not in sent
    a = result.attempts[0]
    assert (a.prompt_tokens, a.completion_tokens) == (11, 3)


async def test_insufficient_answer_escalates_once_by_default() -> None:
    server = FakeServer()
    server.answers = ["nope"]
    result = await server.model().run(
        LocalRunRequest(prompt="x", criteria=Criteria(must_contain=["pong"])),
        escalate_to="codex",
    )
    assert result.verdict == "escalate" and result.escalate_to == "codex"
    assert result.text is None
    assert len(server.chat) == 1  # max_attempts=1: no retry


async def test_second_attempt_can_succeed_and_attempts_are_capped() -> None:
    server = FakeServer()
    server.answers = ["nope", "pong"]
    ok = await server.model().run(
        LocalRunRequest(
            prompt="x", criteria=Criteria(must_contain=["pong"]), max_attempts=2
        ),
        escalate_to="codex",
    )
    assert ok.verdict == "accepted" and len(ok.attempts) == 2

    server = FakeServer()
    server.answers = ["nope"]
    capped = await server.model().run(
        LocalRunRequest(
            prompt="x", criteria=Criteria(must_contain=["pong"]), max_attempts=2
        ),
        escalate_to="codex",
    )
    assert capped.verdict == "escalate" and len(server.chat) == 2


async def test_budget_exhaustion_is_not_accepted() -> None:
    server = FakeServer()
    server.finish = "length"
    result = await server.model().run(
        LocalRunRequest(prompt="x", criteria=Criteria(must_contain=["x"])),
        escalate_to="codex",
    )
    assert result.verdict == "escalate"
    assert "budget exhausted" in result.attempts[0].failures[-1]


async def test_server_error_escalates_instead_of_raising() -> None:
    server = FakeServer()
    server.answers = [httpx.ReadTimeout("slow")]
    result = await server.model().run(
        LocalRunRequest(prompt="x", criteria=Criteria(must_contain=["x"])),
        escalate_to="codex",
    )
    assert result.verdict == "escalate"
    assert "ReadTimeout" in result.attempts[0].failures[0]


async def test_schema_criteria_become_a_structured_output_request() -> None:
    server = FakeServer()
    server.answers = ['{"a": 1}']
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "required": ["a"],
    }
    result = await server.model().run(
        LocalRunRequest(
            prompt="x",
            criteria=Criteria(json_schema=schema, must_contain=["1"]),
        ),
        escalate_to="codex",
    )
    assert result.verdict == "accepted"
    fmt = server.chat[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["schema"] == schema


# -- routing ----------------------------------------------------------------------------

INFO = LocalModelInfo(reachable=True, model=MODEL, n_ctx=65536, slots=1)
EASY = RouteRequest(
    summary="classify",
    simple=True,
    low_risk=True,
    criteria_deterministic=True,
    input_chars=4000,
)


def cat(*, local_ok: bool = True, others: tuple[str, ...] = ("codex", "claude")):
    ledger = full_ledger(*others)
    identity = local_identity("http://local.test:8080", MODEL, INFO.n_ctx)
    if local_ok:
        ledger["local"] = {
            "validated": ok_fact("real run"),
            "validation_identity": identity,
        }
    return catalog.build(
        ledger,
        INFO,
        MODEL,
        local_identity=identity,
        local_validated_event=(
            Fact(ok=True, detail="test probe", at=datetime(2026, 1, 1, tzinfo=UTC))
            if local_ok
            else None
        ),
    )


def test_simple_fitting_task_goes_local_with_escalation_target() -> None:
    d = router.route(EASY, cat(), chat_template_overhead=512)
    assert d.target == "local" and d.model == MODEL
    assert d.estimated_input_tokens == 4000 * 4 + 512 and d.context_tokens == 65536
    assert d.escalate_to == "codex" and d.blocked is None


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"simple": False}, "not declared simple"),
        ({"low_risk": False}, "not declared low risk"),
        ({"criteria_deterministic": False}, "deterministic acceptance"),
        ({"multi_file": True}, "multi-file"),
        ({"touches_sensitive": True}, "auth, security or secrets"),
        ({"input_chars": None}, "context size unknown"),
        ({"input_chars": 200_000}, "does not fit"),
    ],
)
def test_anything_unproven_goes_to_a_real_engine(change: dict, why: str) -> None:
    d = router.route(EASY.model_copy(update=change), cat())
    assert d.target == "codex" and why in d.reason


def test_code_runs_on_codex_and_names_claude_as_executor() -> None:
    d = router.route(
        EASY.model_copy(update={"is_code": True, "multi_file": True}), cat()
    )
    assert d.target == "codex" and d.executor == "claude"


def test_budget_counts_the_output_reserve_and_never_truncates() -> None:
    # 65536 * 0.5 = 32768 input budget; estimate = chars*4 + 512 overhead
    oh = 512
    fits = EASY.model_copy(update={"input_chars": 1000})
    assert router.route(fits, cat(), chat_template_overhead=oh).target == "local"
    over = EASY.model_copy(update={"input_chars": 9000})
    assert router.route(over, cat(), chat_template_overhead=oh).target == "codex"
    big_out = fits.model_copy(update={"output_tokens": 30000})
    assert router.route(big_out, cat(), chat_template_overhead=oh).target == "codex"


def test_unicode_and_adversarial_ascii_upper_bounds() -> None:
    oh = 0
    cjk = "字" * 100
    assert router.estimate_tokens_from_text(cjk, chat_template_overhead=oh) == 300
    assert router.estimate_tokens(100, chat_template_overhead=oh) == 400
    dense = "a" * 100
    assert router.estimate_tokens_from_text(dense, chat_template_overhead=oh) == 100
    d = router.route(
        EASY.model_copy(update={"input_chars": 8000}),
        cat(),
        chat_template_overhead=512,
    )
    assert d.target == "codex" and "does not fit" in d.reason


def test_run_local_budget_counts_serialized_payload() -> None:
    oh = 512
    req = LocalRunRequest(
        prompt="x",
        system="y",
        criteria=Criteria(
            must_contain=["a"],
            json_schema={
                "type": "object",
                "properties": {"label": {"type": "string", "enum": ["pos"]}},
                "required": ["label"],
            },
        ),
    )
    payload = build_chat_completion_payload(req, MODEL)
    est = router.estimate_local_run_input_tokens(
        req, MODEL, payload, chat_template_overhead=oh
    )
    assert est > len(b"xy") + oh


def test_schema_definition_rejects_pattern() -> None:
    with pytest.raises(ValueError, match="pattern"):
        assert_schema_definition(
            {"type": "string", "pattern": "^x+$"},
        )


def test_unvalidated_local_model_is_not_trusted() -> None:
    d = router.route(EASY, cat(local_ok=False))
    assert d.target == "codex" and "not validated" in d.reason


def test_unknown_served_context_routes_away() -> None:
    unknown = LocalModelInfo(reachable=True, model=MODEL, n_ctx=None)
    ledger = full_ledger("codex")
    identity = local_identity("http://local.test:8080", MODEL, None)
    ledger["local"] = {"validated": ok_fact(), "validation_identity": identity}
    d = router.route(
        EASY,
        catalog.build(
            ledger,
            unknown,
            MODEL,
            local_identity=identity,
            local_validated_event=Fact(ok=True, detail="probe"),
        ),
    )
    assert d.target == "codex" and "served context size unknown" in d.reason


def test_unusable_target_is_reported_blocked_not_swapped() -> None:
    # codex is installed but not authenticated: the route says so
    ledger = {"codex": {"installed": ok_fact()}}
    hard = EASY.model_copy(update={"simple": False})
    d = router.route(hard, catalog.build(ledger, None, None))
    assert d.target == "codex" and d.blocked and "authenticated" in d.blocked


def test_no_local_model_configured() -> None:
    d = router.route(EASY, catalog.build({}, None, None))
    assert d.target == "codex" and not d.local_considered


# -- catalogue --------------------------------------------------------------------------


def test_readiness_facts_are_separate() -> None:
    ledger = {"opencode": {"installed": ok_fact("opencode 1.18.27")}}
    entry = next(
        e for e in catalog.build(ledger, None, None).engines if e.id == "opencode"
    )
    r = entry.readiness
    assert r.installed.ok is True
    assert r.authenticated.ok is None and r.validated.ok is None
    assert entry.usable is False


def test_ledger_missing_or_corrupt_means_unknown(tmp_path: Path) -> None:
    assert catalog.read_ledger(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert catalog.read_ledger(bad) == {}
    garbage = {"codex": {"installed": {"ok": "maybe-not-a-bool"}}}
    entry = catalog.build(garbage, None, None).engines[0]
    assert entry.readiness.installed.ok is None


def test_axes_keep_provider_engine_executor_and_account_apart() -> None:
    by_id = {e.id: e for e in catalog.build({}, INFO, MODEL).engines}
    assert by_id["codex"].axes.model_provider == "openai"
    assert by_id["codex"].axes.executor == "codex"
    assert by_id["claude"].role == "code_executor"
    assert by_id["cursor-agent"].axes.executor == "agent"
    assert by_id["local"].axes.executor is None
    assert by_id["local"].axes.model == MODEL


# -- REST and MCP -----------------------------------------------------------------------


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: FakeServer
) -> Generator[TestClient]:
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN["Authorization"].split()[1])
    monkeypatch.setenv("ARGUS_LOCAL_MODEL_URL", "http://local.test:8080")
    monkeypatch.setenv("ARGUS_LOCAL_MODEL", MODEL)
    (tmp_path / "engines-ledger.json").write_text(
        json.dumps({"engines": full_ledger("codex", "claude")})
    )
    with TestClient(app) as c:
        app.state.local_model = server.model()
        yield c


def seed_local_validation(client: TestClient) -> None:
    res = client.post(
        "/api/engines/local/run",
        json={"prompt": "say pong", "criteria": {"must_contain": ["pong"]}},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    assert res.json()["verdict"] == "accepted"


def events_of(client: TestClient, kind: str) -> list[dict]:
    res = client.get("/api/events", params={"kind": kind}, headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    return body if isinstance(body, list) else body.get("events", body.get("items", []))


def test_catalogue_is_read_only_and_does_not_log_events(client: TestClient) -> None:
    worker = bearer(agent_token(client, "worker"))
    before = len(events_of(client, "engine.routed")) + len(
        events_of(client, "engine.local_run")
    )
    for _ in range(2):
        res = client.get("/api/engines", headers=worker)
        assert res.status_code == 200
    after = len(events_of(client, "engine.routed")) + len(
        events_of(client, "engine.local_run")
    )
    assert before == after
    assert len(client.get("/api/engines", headers=worker).json()["engines"]) == 5


def test_catalogue_over_rest_for_any_token(client: TestClient) -> None:
    worker = bearer(agent_token(client, "worker"))
    res = client.get("/api/engines", headers=worker)
    assert res.status_code == 200
    body = res.json()
    ids = [e["id"] for e in body["engines"]]
    assert ids == ["codex", "claude", "opencode", "cursor-agent", "local"]
    assert body["local"]["n_ctx"] == 65536
    assert client.get("/api/engines").status_code == 401
    # no secret-looking field anywhere
    assert "token" not in json.dumps(body).lower()


def test_route_needs_a_manager_and_is_recorded(client: TestClient) -> None:
    seed_local_validation(client)
    worker = bearer(agent_token(client, "worker"))
    body = {**EASY.model_dump(), "summary": "classify"}
    assert (
        client.post("/api/engines/route", json=body, headers=worker).status_code == 403
    )
    res = client.post("/api/engines/route", json=body, headers=ADMIN)
    assert res.status_code == 200, res.text
    assert res.json()["target"] == "local"
    logged = client.get("/api/events", headers=ADMIN).text
    assert "engine.routed" in logged


def test_local_run_over_rest_records_usage_and_unknown_cost(
    client: TestClient, server: FakeServer
) -> None:
    worker = bearer(agent_token(client, "worker"))
    res = client.post(
        "/api/engines/local/run",
        json={"prompt": "say pong", "criteria": {"must_contain": ["pong"]}},
        headers=worker,
    )
    assert res.status_code == 200, res.text
    assert res.json()["verdict"] == "accepted" and res.json()["cost"] is None
    logged = client.get("/api/events", headers=ADMIN).text
    assert "engine.local_run" in logged and '"cost":null' in logged.replace(
        " ", ""
    ).replace("\\", "")


def test_local_run_without_a_configured_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.state.local_model = None
    res = client.post(
        "/api/engines/local/run",
        json={"prompt": "x", "criteria": {"must_contain": ["x"]}},
        headers=ADMIN,
    )
    assert (
        res.status_code == 409
        and res.json()["detail"]["code"] == "local_model_not_configured"
    )


def test_escalation_example_end_to_end(client: TestClient, server: FakeServer) -> None:
    """Route says local; the local answer fails the criteria; the verdict names
    the escalation target and nothing else ran."""
    seed_local_validation(client)
    server.answers = ["I don't know"]
    decision = client.post(
        "/api/engines/route", json={**EASY.model_dump()}, headers=ADMIN
    ).json()
    assert decision["target"] == "local"
    run = client.post(
        "/api/engines/local/run",
        json={
            "prompt": "label it",
            "criteria": {"must_contain": ["definitely-not-in-answer"]},
        },
        headers=ADMIN,
    ).json()
    assert (
        run["verdict"] == "escalate" and run["escalate_to"] == decision["escalate_to"]
    )
    assert len(server.chat) == 2


def test_run_local_rejects_oversized_prompt_without_calling_the_model(
    client: TestClient, server: FakeServer
) -> None:
    huge = "字" * 40000
    res = client.post(
        "/api/engines/local/run",
        json={"prompt": huge, "criteria": {"must_contain": ["x"]}},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["verdict"] == "escalate" and body["escalate_to"] == "codex"
    assert "does not fit" in body["attempts"][0]["failures"][0]
    assert server.chat == []


def test_mcp_tools(client: TestClient) -> None:
    orch = agent_token(client, "orchestrator")
    engines = tool(client, orch, "list_engines")
    assert {e["id"] for e in engines["engines"]} >= {"codex", "local"}
    run = tool(
        client,
        orch,
        "run_local",
        prompt="say pong",
        criteria={"must_contain": ["pong"]},
    )
    assert run["verdict"] == "accepted"
    engines = tool(client, orch, "list_engines")
    local = next(e for e in engines["engines"] if e["id"] == "local")
    assert local["readiness"]["validated"]["ok"] is True
    decision = tool(
        client,
        orch,
        "route_task",
        summary="x",
        **{k: v for k, v in EASY.model_dump().items() if k != "summary"},
    )
    assert decision["target"] == "local"
    worker = agent_token(client, "worker")
    assert "error" in tool(client, worker, "route_task", summary="x")


def test_local_validated_from_accepted_run_without_ledger_seed(
    client: TestClient,
) -> None:
    before = client.get("/api/engines", headers=ADMIN).json()
    local = next(e for e in before["engines"] if e["id"] == "local")
    assert local["readiness"]["validated"]["ok"] is not True
    seed_local_validation(client)
    after = client.get("/api/engines", headers=ADMIN).json()
    local = next(e for e in after["engines"] if e["id"] == "local")
    assert local["readiness"]["validated"]["ok"] is True
    route = client.post(
        "/api/engines/route", json={**EASY.model_dump()}, headers=ADMIN
    ).json()
    assert route["target"] == "local"
    second = client.get("/api/engines", headers=ADMIN).json()
    assert (
        next(e for e in second["engines"] if e["id"] == "local")["readiness"][
            "validated"
        ]["ok"]
        is True
    )


def test_escalated_local_run_does_not_set_validated(
    client: TestClient, server: FakeServer
) -> None:
    server.answers = ["not pong"]
    res = client.post(
        "/api/engines/local/run",
        json={
            "prompt": "say pong",
            "criteria": {"must_contain": ["definitely-missing"]},
        },
        headers=ADMIN,
    )
    assert res.json()["verdict"] == "escalate"
    local = next(
        e
        for e in client.get("/api/engines", headers=ADMIN).json()["engines"]
        if e["id"] == "local"
    )
    assert local["readiness"]["validated"]["ok"] is not True


def test_served_model_change_invalidates_event_validation(
    client: TestClient, server: FakeServer
) -> None:
    seed_local_validation(client)
    server.served = "other-model"
    app.state.local_model = server.model()
    local = next(
        e
        for e in client.get("/api/engines", headers=ADMIN).json()["engines"]
        if e["id"] == "local"
    )
    assert local["readiness"]["validated"]["ok"] is not True
    route = client.post(
        "/api/engines/route", json={**EASY.model_dump()}, headers=ADMIN
    ).json()
    assert route["target"] == "codex" and route["target"] != "local"


def test_served_context_change_invalidates_event_validation(
    client: TestClient, server: FakeServer
) -> None:
    seed_local_validation(client)
    server.n_ctx = 8192
    app.state.local_model = server.model()
    local = next(
        e
        for e in client.get("/api/engines", headers=ADMIN).json()["engines"]
        if e["id"] == "local"
    )
    assert local["readiness"]["validated"]["ok"] is not True


def test_ledger_validated_without_identity_is_ignored(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "engines-ledger.json"
    ledger.write_text(
        json.dumps({"engines": {"local": {"validated": ok_fact("smoke only")}}})
    )
    monkeypatch.setenv("ARGUS_ENGINES_LEDGER", str(ledger))
    local = next(
        e
        for e in client.get("/api/engines", headers=ADMIN).json()["engines"]
        if e["id"] == "local"
    )
    assert local["readiness"]["validated"]["ok"] is not True


REAL_LOCAL_OPT_IN = "ARGUS_TEST_REAL_LOCAL_MODEL"


def _real_local_requested() -> bool:
    return os.environ.get(REAL_LOCAL_OPT_IN) == "1"


def _real_local_url() -> str:
    return os.environ.get("ARGUS_TEST_LOCAL_MODEL_URL", "http://127.0.0.1:8080")


def _real_local_model() -> str:
    return os.environ.get("ARGUS_TEST_LOCAL_MODEL", "qwen3.8-flash-next-iq3_xxs")


def _loopback_model_served() -> bool:
    try:
        url = _real_local_url()
        res = httpx.get(f"{url}/v1/models", timeout=3.0)
        res.raise_for_status()
        ids = [m.get("id") for m in res.json().get("data", [])]
        return _real_local_model() in ids
    except (httpx.HTTPError, ValueError):
        return False


@pytest.fixture
def loopback_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient]:
    if not _real_local_requested():
        pytest.skip(
            f"set {REAL_LOCAL_OPT_IN}=1 for loopback integration (hermetic by default)"
        )
    if not _loopback_model_served():
        pytest.fail(
            f"{REAL_LOCAL_OPT_IN}=1 but {_real_local_model()!r} not served on {_real_local_url()}"
        )
    for name in list(os.environ):
        if name.startswith(("ARGUS_", "DOCKER_HOST")) and not name.startswith(
            "ARGUS_TEST_"
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN["Authorization"].split()[1])
    monkeypatch.setenv("ARGUS_LOCAL_MODEL_URL", _real_local_url())
    monkeypatch.setenv("ARGUS_LOCAL_MODEL", _real_local_model())
    ledger = tmp_path / "engines-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "engines": {
                    "codex": {"installed": ok_fact("0.162.1")},
                    "claude": {"installed": ok_fact("2.1.296")},
                }
            }
        )
    )
    monkeypatch.setenv("ARGUS_ENGINES_LEDGER", str(ledger))
    with TestClient(app) as c:
        yield c


def test_loopback_catalogue_reports_served_context(loopback_client: TestClient) -> None:
    res = loopback_client.get("/api/engines", headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    local = next(e for e in body["engines"] if e["id"] == "local")
    assert local["readiness"]["installed"]["ok"] is True
    assert body["local"]["n_ctx"] == 65536
    codex = next(e for e in body["engines"] if e["id"] == "codex")
    assert codex["readiness"]["authenticated"]["ok"] is not True


def _record_loopback(case: str, body: dict) -> None:
    artifact = os.environ.get("ARGUS_TEST_LOOPBACK_ARTIFACT")
    if not artifact:
        return
    path = Path(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = {}
    if path.exists():
        prior = json.loads(path.read_text())
    attempt = body["attempts"][-1] if body.get("attempts") else {}
    prior[case] = {
        "verdict": body.get("verdict"),
        "model": body.get("model"),
        "text": body.get("text"),
        "prompt_tokens": attempt.get("prompt_tokens"),
        "completion_tokens": attempt.get("completion_tokens"),
        "latency_ms": attempt.get("latency_ms"),
        "failures": attempt.get("failures"),
    }
    path.write_text(json.dumps(prior, indent=2))


def test_loopback_real_run_accepted_with_evidence(loopback_client: TestClient) -> None:
    run = loopback_client.post(
        "/api/engines/local/run",
        json={
            "prompt": 'Reply with exactly the word "pong" and nothing else.',
            "criteria": {"must_contain": ["pong"], "max_chars": 80},
            "max_output_tokens": 128,
        },
        headers=ADMIN,
    )
    assert run.status_code == 200, run.text
    body = run.json()
    _record_loopback("accepted_pong", body)
    assert body["verdict"] == "accepted", body
    assert body["model"] == _real_local_model()
    assert "pong" in (body["text"] or "").lower()
    attempt = body["attempts"][-1]
    assert attempt["prompt_tokens"] is not None and attempt["prompt_tokens"] > 0
    assert attempt["latency_ms"] > 0


def test_loopback_real_run_escalates_on_impossible_equals(
    loopback_client: TestClient,
) -> None:
    token = "ARGUS_ESCALATE_PROBE_XYZZY"
    run = loopback_client.post(
        "/api/engines/local/run",
        json={
            "prompt": "Say hello briefly.",
            "criteria": {"equals": token},
            "max_output_tokens": 128,
        },
        headers=ADMIN,
    )
    assert run.status_code == 200, run.text
    body = run.json()
    _record_loopback("escalated_impossible_equals", body)
    assert body["verdict"] == "escalate"
    assert body["escalate_to"] == "codex"
    assert body["text"] is None
    assert body["attempts"][-1]["failures"]


def test_loopback_mcp_orchestrator_envelope(loopback_client: TestClient) -> None:
    orch = agent_token(loopback_client, "orchestrator")
    engines = tool(loopback_client, orch, "list_engines")
    assert _real_local_model() in json.dumps(engines)
    run = tool(
        loopback_client,
        orch,
        "run_local",
        prompt='Say "pong" only.',
        criteria={"must_contain": ["pong"], "max_chars": 40},
        max_output_tokens=128,
    )
    assert run["verdict"] == "accepted", run
    engines = tool(loopback_client, orch, "list_engines")
    local = next(e for e in engines["engines"] if e["id"] == "local")
    assert local["readiness"]["validated"]["ok"] is True
    decision = tool(
        loopback_client,
        orch,
        "route_task",
        summary="pong",
        simple=True,
        low_risk=True,
        criteria_deterministic=True,
        input_chars=100,
    )
    assert decision["target"] == "local"
    assert run["model"] == _real_local_model()
    assert "pong" in (run.get("text") or "").lower()
