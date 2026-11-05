"""Local accounts as the Argus API process sees them (local harness spec): the
endpoint per runtime, discovery and its failure classes, per-account health, the
admin test and editor, and what a lease delivers to each runtime.

Every network call here runs outside a write transaction; what it learned is
written afterwards or kept in memory. Nothing reads or repeats a server's text:
only Argus's own exception types, the HTTP status and model ids that pass the
model-name rule.
"""

import asyncio
import socket
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import aiosqlite
import httpx2 as httpx
from argus_core.accounts import (
    LOCAL_AUTH_TOKEN,
    LOCAL_PROVIDER,
    AccountLease,
    LocalAccountUpdate,
    LocalAccountView,
    LocalCheck,
    LocalEndpointSource,
    LocalFailure,
    LocalTestRequest,
    LocalTestResult,
    OrchestratorModelSource,
    is_model_name,
    local_api_endpoint,
    local_credential_problem,
    local_worker_credential,
    validate_local_base_url,
)

from argus_api.db import Database, ts, utcnow
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.events import record
from argus_api.secrets import SecretStore, ref_name

DOCKERENV = Path("/.dockerenv")
DISCOVERY_TIMEOUT_S = 5.0
DISCOVERY_TTL_S = 30.0
COMPLETION_TIMEOUT_S = 120.0
# One bounded output budget for both test completions: a reasoning model spends
# tokens on `reasoning_content` before it answers, so a tiny budget would end in
# an empty reply and say nothing about the model.
TEST_MAX_TOKENS = 256
TEST_TOOL = "report_status"
# Argus disables a local account for exactly these reasons, and only its own
# probe brings it back (accounts service `sync_local_health`).
LOCAL_UNREACHABLE = "local model unreachable"
LOCAL_UNAVAILABLE = "local model unavailable"
HEALTH_REASONS = (LOCAL_UNREACHABLE, LOCAL_UNAVAILABLE)
Runtime = Literal["api", "worker"]


def in_container() -> bool:
    """Does the Argus API process run in a container? (`/.dockerenv`.)"""
    return DOCKERENV.exists()


def origin(url: str) -> str:
    """Scheme, host and port: the only part of an endpoint replies and logs show."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


# -- failures ---------------------------------------------------------------------


def classify(exc: BaseException) -> LocalFailure:
    """The failure class of a transport error, from the exception chain of Argus's
    own client (type and errno), never its message."""
    seen: set[int] = set()
    node: BaseException | None = exc
    timeout = protocol = False
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, socket.gaierror):
            return "dns_failed"
        if isinstance(node, ConnectionRefusedError):
            return "connection_refused"
        if isinstance(node, ssl.SSLError):
            return "tls_failed"
        if isinstance(node, (httpx.TimeoutException, TimeoutError)):
            timeout = True
        if isinstance(node, (httpx.RemoteProtocolError, httpx.DecodingError)):
            protocol = True
        node = node.__cause__ or node.__context__
    if timeout:
        return "timeout"
    if protocol:
        return "invalid_response"
    return "connect_failed"


def failure_sentence(
    failure: LocalFailure,
    endpoint: str,
    *,
    http_status: int | None = None,
    local: bool = True,
    model: str | None = None,
    cut: bool = False,
) -> str:
    """Argus's own words for a failure, with what to change. Never server text."""
    where = f"`{origin(endpoint)}`"
    host = urlsplit(endpoint).hostname or endpoint
    fix = (
        " Set the account's API endpoint in Accounts → Local endpoints, then test it"
        " there."
        if local
        else ""
    )
    match failure:
        case "dns_failed":
            return f"`{host}` does not resolve from the Argus API process.{fix}"
        case "connection_refused":
            return f"Nothing is listening at {where}: start the model server or check the port."
        case "timeout":
            return f"{where} did not answer in time."
        case "tls_failed":
            return f"The TLS handshake with {where} failed."
        case "redirect_refused":
            return f"{where} answered with a redirect, which Argus never follows."
        case "http_error":
            return f"{where} answered HTTP {http_status or 'error'}."
        case "invalid_response":
            return (
                f"{where} answered with a response Argus cannot read: is it an"
                " OpenAI-compatible server?"
            )
        case "no_models":
            return f"{where} lists no model."
        case "model_not_served":
            return f"`{model}` is not served at {where}."
        case "empty_answer":
            return f"`{model}` at {where} returned no answer" + (
                f" within {TEST_MAX_TOKENS} output tokens (a reasoning model may"
                " spend them all on reasoning)."
                if cut
                else "."
            )
        case "no_tool_call":
            return (
                f"`{model}` at {where} answered without calling the offered tool: the"
                " built-in orchestrator needs a model with tool calling."
            )
        case _:
            return f"{where} could not be reached.{fix}"


# -- discovery --------------------------------------------------------------------


@dataclass(frozen=True)
class Discovery:
    """One `GET /v1/models` from the API process."""

    endpoint: str
    failure: LocalFailure | None
    http_status: int | None
    models: tuple[str, ...]
    n_ctx: Mapping[str, int]
    props_n_ctx: int | None
    latency_ms: int
    at: datetime

    @property
    def ok(self) -> bool:
        return self.failure is None

    def window(self, model: str | None) -> int | None:
        if model and model in self.n_ctx:
            return self.n_ctx[model]
        if model is None and self.models and self.models[0] in self.n_ctx:
            return self.n_ctx[self.models[0]]
        return self.props_n_ctx

    def check(self) -> LocalCheck:
        if self.failure is not None:
            return LocalCheck(
                status="failed",
                failure=self.failure,
                http_status=self.http_status,
                detail=failure_sentence(
                    self.failure, self.endpoint, http_status=self.http_status
                ),
                latency_ms=self.latency_ms,
                at=self.at,
            )
        if not self.models:
            return LocalCheck(
                status="failed",
                failure="no_models",
                detail=failure_sentence("no_models", self.endpoint),
                latency_ms=self.latency_ms,
                at=self.at,
            )
        return LocalCheck(status="passed", latency_ms=self.latency_ms, at=self.at)


def _ctx(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def discover(client: httpx.AsyncClient, endpoint: str, token: str) -> Discovery:
    started = time.monotonic()
    status: int | None = None
    failure: LocalFailure | None = None
    models: list[str] = []
    n_ctx: dict[str, int] = {}
    props: int | None = None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = await client.get(
            f"{endpoint}/v1/models", headers=headers, timeout=DISCOVERY_TIMEOUT_S
        )
        status = res.status_code
        if res.is_redirect:
            failure = "redirect_refused"
        elif status != 200:
            failure = "http_error"
        else:
            body: Any = res.json()
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list):
                failure = "invalid_response"
            else:
                for item in data:
                    if isinstance(item, dict) and is_model_name(item.get("id")):
                        models.append(item["id"])
                        meta = item.get("meta")
                        ctx = (
                            _ctx(meta.get("n_ctx")) if isinstance(meta, dict) else None
                        )
                        if ctx:
                            n_ctx[item["id"]] = ctx
        if failure is None and models and not n_ctx:
            props = await _props_window(client, endpoint, headers)
    except (httpx.HTTPError, OSError) as exc:
        failure = classify(exc)
    except ValueError:
        failure = "invalid_response"
    return Discovery(
        endpoint=endpoint,
        failure=failure,
        http_status=status,
        models=tuple(models),
        n_ctx=n_ctx,
        props_n_ctx=props,
        latency_ms=int((time.monotonic() - started) * 1000),
        at=utcnow(),
    )


async def _props_window(
    client: httpx.AsyncClient, endpoint: str, headers: dict[str, str]
) -> int | None:
    """llama.cpp's `/props` context window; optional, any problem is no figure."""
    try:
        res = await client.get(
            f"{endpoint}/props", headers=headers, timeout=DISCOVERY_TIMEOUT_S
        )
        if res.status_code != 200:
            return None
        body = res.json()
    except (httpx.HTTPError, OSError, ValueError):
        return None
    settings = (
        body.get("default_generation_settings") if isinstance(body, dict) else None
    )
    return _ctx(settings.get("n_ctx")) if isinstance(settings, dict) else None


# -- one account ------------------------------------------------------------------


@dataclass(frozen=True)
class LocalAccount:
    """A local account's stored document, resolved for the API process."""

    id: str
    name: str
    credential: dict[str, Any]
    api_endpoint: str
    source: LocalEndpointSource

    @property
    def base_url(self) -> str:
        return validate_local_base_url(self.credential["base_url"])

    @property
    def model(self) -> str | None:
        model = self.credential.get("model")
        return model if isinstance(model, str) and model else None

    @property
    def token(self) -> str:
        token = self.credential.get("auth_token")
        return token if isinstance(token, str) and token.strip() else LOCAL_AUTH_TOKEN

    def identity(self, override: str | None) -> tuple[Any, ...]:
        """What a test result describes: it stops applying when any part changes."""
        return (
            self.api_endpoint,
            self.base_url,
            self.model,
            self.credential.get("context_tokens"),
            override,
        )


def resolve(
    account_id: str,
    name: str,
    credential: dict[str, Any] | None,
    *,
    container: bool,
) -> LocalAccount | None:
    """None when the stored document is missing or not a valid local credential."""
    if credential is None or local_credential_problem(credential) is not None:
        return None
    endpoint, source = local_api_endpoint(credential, in_container=container)
    return LocalAccount(account_id, name, dict(credential), endpoint, source)


def worker_model(account: LocalAccount, found: Discovery | None) -> str | None:
    """What workers run: the account's model, else the first served one."""
    if account.model:
        return account.model
    return found.models[0] if found and found.models else None


def orchestrator_model(
    account: LocalAccount, override: str | None, found: Discovery | None
) -> tuple[str | None, OrchestratorModelSource | None]:
    """What the built-in orchestrator runs on this account (builtin-orchestrator
    spec 3.4): `orchestrator.model`'s `local/<model>`, the account's, the served."""
    if override:
        return override, "orchestrator.model"
    if account.model:
        return account.model, "account"
    if found and found.models:
        return found.models[0], "served"
    return None, None


def verdict(
    account: LocalAccount, override: str | None, found: Discovery | None
) -> tuple[bool | None, str]:
    """The account's health from its own discovery (local harness spec, "Health
    per account"): (healthy, disabled reason). Not probed: (None, "")."""
    if found is None:
        return None, ""
    where = origin(found.endpoint)
    if found.failure is not None:
        what = (
            f"http_error {found.http_status}"
            if found.failure == "http_error"
            else found.failure
        )
        return False, f"{LOCAL_UNREACHABLE}: {what} at {where}"
    if not found.models:
        return False, f"{LOCAL_UNAVAILABLE}: no model is served at {where}"
    served = set(found.models)
    workers_ok = account.model in served if account.model else True
    if workers_ok or (override is not None and override in served):
        return True, ""
    named = ", ".join(f"`{m}`" for m in (account.model, override) if m)
    return False, f"{LOCAL_UNAVAILABLE}: {named} not served at {where}"[:500]


def warnings(
    account: LocalAccount, override: str | None, found: Discovery | None
) -> list[str]:
    """Stale models: a model the account runs that the server no longer lists."""
    if found is None or not found.ok or not found.models:
        return []
    served = ", ".join(f"`{m}`" for m in found.models[:10])
    where = f"`{origin(found.endpoint)}`"
    out = []
    if account.model and account.model not in found.models:
        out.append(
            f"Stale model: the account's model `{account.model}` (what workers run)"
            f" is not served at {where}, which serves {served}. Pick a served model"
            " or clear it."
        )
    if override and override not in found.models:
        out.append(
            f"Stale model: `orchestrator.model` names `{override}`, which is not"
            f" served at {where} ({served}). Change it in Settings."
        )
    return out


# -- the in-memory record ----------------------------------------------------------


@dataclass
class LocalHealth:
    """The API process's latest discovery per endpoint and last admin test per
    account. Memory only: a restart forgets it, and the next probe refills it."""

    transport: httpx.AsyncBaseTransport | None = None
    ttl_s: float = DISCOVERY_TTL_S
    clock: Any = time.monotonic
    container: bool = field(default_factory=in_container)
    _found: dict[str, tuple[float, Discovery]] = field(default_factory=dict)
    _tests: dict[str, tuple[tuple[Any, ...], LocalTestResult]] = field(
        default_factory=dict
    )

    def client(self, timeout: float = DISCOVERY_TIMEOUT_S) -> httpx.AsyncClient:
        # Redirects are refused, TLS is verified, no proxy from the environment:
        # the endpoint is on this node by construction.
        return httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def last(self, endpoint: str) -> Discovery | None:
        hit = self._found.get(endpoint)
        return hit[1] if hit else None

    async def discover(
        self, endpoints: Mapping[str, str], *, fresh: bool = False
    ) -> dict[str, Discovery]:
        """Discovery of each endpoint (to its placeholder token), cached for `ttl_s`
        unless `fresh`. Endpoints are probed concurrently, each once."""
        now = self.clock()
        due = {
            e: t
            for e, t in endpoints.items()
            if fresh or e not in self._found or now - self._found[e][0] >= self.ttl_s
        }
        if due:
            async with self.client() as client:
                found = await asyncio.gather(
                    *(discover(client, e, t) for e, t in due.items())
                )
            for item in found:
                self._found[item.endpoint] = (self.clock(), item)
        return {e: self._found[e][1] for e in endpoints}

    def remember(
        self, account: LocalAccount, override: str | None, result: LocalTestResult
    ) -> None:
        self._tests[account.id] = (account.identity(override), result)

    def last_test(
        self, account: LocalAccount, override: str | None
    ) -> LocalTestResult | None:
        hit = self._tests.get(account.id)
        if hit is None or hit[0] != account.identity(override):
            return None
        return hit[1]

    def forget(self, account_id: str) -> None:
        self._tests.pop(account_id, None)


# -- reading accounts and the orchestrator's model ---------------------------------


async def orchestrator_override(
    conn: aiosqlite.Connection, model_policy_path: Path
) -> str | None:
    """The model `orchestrator.model` names for `local`, when it names one."""
    from argus_api.accounts.service import (
        ORCHESTRATOR_MODEL_KEY,
        orchestrator_preference,
    )
    from argus_api.settings import service as settings_service

    setting = await settings_service.get_setting(
        conn, model_policy_path, settings_service.GLOBAL, ORCHESTRATOR_MODEL_KEY
    )
    prefer = orchestrator_preference(setting.value)
    if prefer is None or prefer.provider != LOCAL_PROVIDER:
        return None
    return prefer.model


async def local_accounts(
    conn: aiosqlite.Connection, store: SecretStore, *, container: bool
) -> list[LocalAccount]:
    async with conn.execute(
        "SELECT id, name, secret_ref FROM accounts WHERE provider = ? ORDER BY name",
        (LOCAL_PROVIDER,),
    ) as cur:
        rows = await cur.fetchall()
    out = []
    for row in rows:
        secret = ref_name(row["secret_ref"])
        found = resolve(
            row["id"],
            row["name"],
            store.get(secret) if secret else None,
            container=container,
        )
        if found is not None:
            out.append(found)
    return out


async def probe_all(
    db: Database, store: SecretStore, health: LocalHealth, model_policy_path: Path
) -> dict[str, tuple[bool | None, str]]:
    """Each local account's health from its own API endpoint (the reaper's probe).
    Runs outside any write; the reaper applies it in its transaction."""
    async with db.read() as conn:
        accounts = await local_accounts(conn, store, container=health.container)
        override = await orchestrator_override(conn, model_policy_path)
    if not accounts:
        return {}
    found = await health.discover({a.api_endpoint: a.token for a in accounts})
    return {a.id: verdict(a, override, found[a.api_endpoint]) for a in accounts}


def view(
    account: LocalAccount, override: str | None, health: LocalHealth
) -> LocalAccountView:
    found = health.last(account.api_endpoint)
    model, source = orchestrator_model(account, override, found)
    test = health.last_test(account, override)
    ctx = account.credential.get("context_tokens")
    return LocalAccountView(
        base_url=account.base_url,
        api_base_url=account.credential.get("api_base_url"),
        api_endpoint=account.api_endpoint,
        api_endpoint_source=account.source,
        model=account.model,
        context_tokens=ctx if isinstance(ctx, int) else None,
        orchestrator_model=model,
        orchestrator_model_source=source,
        served_models=list(found.models) if found and found.ok else None,
        discovery=_discovery_check(account, override, found) if found else LocalCheck(),
        inference=test.inference if test else LocalCheck(),
        orchestrator=test.orchestrator if test else LocalCheck(),
        warnings=warnings(account, override, found),
    )


def _discovery_check(
    account: LocalAccount, override: str | None, found: Discovery
) -> LocalCheck:
    """Discovery passes when the account would be healthy: the endpoint answers
    and a model it runs is served."""
    check = found.check()
    if check.status != "passed":
        return check
    healthy, _ = verdict(account, override, found)
    if healthy:
        return check
    model = account.model or override
    return check.model_copy(
        update={
            "status": "failed",
            "failure": "model_not_served",
            "model": model,
            "detail": failure_sentence("model_not_served", found.endpoint, model=model),
        }
    )


# -- the admin test ----------------------------------------------------------------


async def _complete(
    client: httpx.AsyncClient,
    account: LocalAccount,
    model: str,
    *,
    tools: bool,
) -> LocalCheck:
    """One short chat completion; `tools` offers one tool, as the built-in does."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Call the {TEST_TOOL} tool with status ok."
                    if tools
                    else "Reply with the single word OK."
                ),
            }
        ],
        "max_tokens": TEST_MAX_TOKENS,
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": TEST_TOOL,
                    "description": "Report a status word.",
                    "parameters": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                    },
                },
            }
        ]
        body["tool_choice"] = "auto"
    endpoint = account.api_endpoint
    started = time.monotonic()

    def done(**kw: Any) -> LocalCheck:
        return LocalCheck(
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            at=utcnow(),
            **kw,
        )

    try:
        res = await client.post(
            f"{endpoint}/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {account.token}"},
            timeout=COMPLETION_TIMEOUT_S,
        )
    except (httpx.HTTPError, OSError) as exc:
        failure = classify(exc)
        return done(
            status="failed", failure=failure, detail=failure_sentence(failure, endpoint)
        )
    if res.is_redirect or res.status_code != 200:
        failure: LocalFailure = "redirect_refused" if res.is_redirect else "http_error"
        detail = failure_sentence(failure, endpoint, http_status=res.status_code)
        if tools and failure == "http_error" and 400 <= res.status_code < 500:
            detail += " The server refused a request with tools: the model may not support tool calls."
        return done(
            status="failed", failure=failure, http_status=res.status_code, detail=detail
        )
    try:
        choice = res.json()["choices"][0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise TypeError
    except (ValueError, KeyError, IndexError, TypeError):
        return done(
            status="failed",
            failure="invalid_response",
            detail=failure_sentence("invalid_response", endpoint),
        )
    cut = choice.get("finish_reason") == "length"
    if not tools:
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return done(status="passed")
        # Only reasoning, or nothing: the model did not answer (never a pass).
        return done(
            status="failed",
            failure="empty_answer",
            detail=failure_sentence("empty_answer", endpoint, model=model, cut=cut),
        )
    calls = message.get("tool_calls")
    names = (
        [(c.get("function") or {}).get("name") for c in calls if isinstance(c, dict)]
        if isinstance(calls, list)
        else []
    )
    if TEST_TOOL in names:
        return done(status="passed")
    if cut:
        return done(
            status="failed",
            failure="empty_answer",
            detail=failure_sentence("empty_answer", endpoint, model=model, cut=True),
        )
    return done(
        status="failed",
        failure="no_tool_call",
        detail=failure_sentence("no_tool_call", endpoint, model=model),
    )


def _skipped(
    failure: LocalFailure, detail: str, model: str | None = None
) -> LocalCheck:
    return LocalCheck(
        status="skipped", failure=failure, detail=detail, model=model, at=utcnow()
    )


async def run_test(
    db: Database,
    store: SecretStore,
    health: LocalHealth,
    model_policy_path: Path,
    account_id: str,
    body: LocalTestRequest,
    actor: str,
) -> LocalTestResult:
    """An admin's test of one local account from the API process (local harness
    spec, "Admin test"): discovery always, each completion only when asked."""
    async with db.read() as conn:
        account = await _local(conn, store, account_id, health.container)
        override = await orchestrator_override(conn, model_policy_path)
    found = (await health.discover({account.api_endpoint: account.token}, fresh=True))[
        account.api_endpoint
    ]
    discovery = _discovery_check(account, override, found)
    reachable = found.ok and bool(found.models)
    not_run = LocalCheck()
    inference = orchestrator = not_run
    if body.inference or body.tool_calling:
        async with health.client(COMPLETION_TIMEOUT_S) as client:
            if body.inference:
                model = worker_model(account, found)
                inference = await _gated(
                    client, account, found, model, reachable, tools=False
                )
            if body.tool_calling:
                model, _ = orchestrator_model(account, override, found)
                orchestrator = await _gated(
                    client, account, found, model, reachable, tools=True
                )
    result = LocalTestResult(
        account_id=account.id,
        account_name=account.name,
        endpoint=account.api_endpoint,
        endpoint_source=account.source,
        worker_endpoint=account.base_url,
        worker_note=(
            None
            if account.base_url == account.api_endpoint
            else f"Workers reach `{account.base_url}` from their containers; the Argus"
            " API process cannot test that address from its own network."
        ),
        served_models=list(found.models),
        discovery=discovery,
        inference=inference,
        orchestrator=orchestrator,
        warnings=warnings(account, override, found),
    )
    health.remember(account, override, result)
    async with db.write() as tx:
        await record(
            tx,
            actor,
            "account",
            account.id,
            "account.local_tested",
            {
                name: {"status": check.status, "failure": check.failure}
                for name, check in (
                    ("discovery", discovery),
                    ("inference", inference),
                    ("orchestrator", orchestrator),
                )
            },
        )
    return result


async def _gated(
    client: httpx.AsyncClient,
    account: LocalAccount,
    found: Discovery,
    model: str | None,
    reachable: bool,
    *,
    tools: bool,
) -> LocalCheck:
    if not reachable:
        return _skipped(
            found.failure or "no_models", "Not sent: discovery failed.", model
        )
    if model is None or model not in found.models:
        return _skipped(
            "model_not_served",
            failure_sentence("model_not_served", found.endpoint, model=model)
            + " Not sent.",
            model,
        )
    return await _complete(client, account, model, tools=tools)


async def _local(
    conn: aiosqlite.Connection, store: SecretStore, account_id: str, container: bool
) -> LocalAccount:
    from argus_api.accounts.service import _row

    row = await _row(conn, account_id)
    if row["provider"] != LOCAL_PROVIDER:
        raise ValidationFailedError("not a local account")
    secret = ref_name(row["secret_ref"])
    account = resolve(
        row["id"],
        row["name"],
        store.get(secret) if secret else None,
        container=container,
    )
    if account is None:
        raise ConflictError(
            "the account's stored endpoint is missing or invalid; replace its"
            " credential",
            code="local_credential_invalid",
        )
    return account


# -- the editor --------------------------------------------------------------------


async def update(
    db: Database,
    store: SecretStore,
    health: LocalHealth,
    account_id: str,
    body: LocalAccountUpdate,
    actor: str,
) -> None:
    """Change a local account's endpoints or model (local harness spec, "Editor").
    The document is validated whole, written to the store, then recorded."""
    from argus_api.accounts.service import _row

    async with db.read() as conn:
        row = await _row(conn, account_id)
    if row["provider"] != LOCAL_PROVIDER:
        raise ValidationFailedError("not a local account")
    secret = ref_name(row["secret_ref"])
    current = store.get(secret) if secret else None
    if secret is None or current is None:
        raise ConflictError(
            "the account's stored endpoint is missing; replace its credential",
            code="local_credential_invalid",
        )
    changed = sorted(body.model_fields_set)
    new = dict(current)
    for key in changed:
        value = getattr(body, key)
        if value is None:
            new.pop(key, None)
        else:
            new[key] = value
    problem = local_credential_problem(new)
    if problem is not None:
        raise ValidationFailedError(problem)
    if new.get("base_url") is not None:
        new["base_url"] = validate_local_base_url(new["base_url"])
    if new.get("api_base_url") is not None:
        new["api_base_url"] = validate_local_base_url(
            new["api_base_url"], "api_base_url"
        )
    await store.put(secret, new)
    health.forget(account_id)
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE accounts SET updated_at = ? WHERE id = ?",
            (ts(tx.now), account_id),
        )
        await record(
            tx,
            actor,
            "account",
            account_id,
            "account.local_updated",
            {"fields": changed},
        )


async def refresh(
    db: Database,
    store: SecretStore,
    health: LocalHealth,
    account_id: str,
) -> None:
    """A fresh discovery of one account (after an edit), no inference."""
    async with db.read() as conn:
        account = await _local(conn, store, account_id, health.container)
    await health.discover({account.api_endpoint: account.token}, fresh=True)


async def views(
    conn: aiosqlite.Connection,
    store: SecretStore,
    health: LocalHealth,
    model_policy_path: Path,
) -> dict[str, LocalAccountView]:
    """The `local` view of every local account, from memory (no network)."""
    accounts = await local_accounts(conn, store, container=health.container)
    if not accounts:
        return {}
    override = await orchestrator_override(conn, model_policy_path)
    return {a.id: view(a, override, health) for a in accounts}


# -- what a lease delivers -----------------------------------------------------------


async def agent_runtime(conn: aiosqlite.Connection, agent_id: str) -> Runtime:
    """`api` for an agent of a built-in pool (it runs in this process), else
    `worker`: every other agent runs in a container or on another node."""
    async with conn.execute(
        "SELECT p.runtime FROM agents a JOIN instances i ON i.id = a.instance_id"
        " JOIN pools p ON p.name = i.pool WHERE a.id = ?",
        (agent_id,),
    ) as cur:
        row = await cur.fetchone()
    return "api" if row and row[0] == "builtin" else "worker"


async def deliver(
    health: LocalHealth, leased: AccountLease, runtime: Runtime
) -> AccountLease:
    """A local lease as `runtime` uses it: its endpoint in `settings.base_url` and
    `credential.base_url`, only the worker keys in the credential, and the
    window and model the account's own probe found when it names none."""
    if leased.provider != LOCAL_PROVIDER:
        return leased
    account = resolve(
        leased.account_id, leased.name, leased.credential, container=health.container
    )
    if account is None:
        return leased.model_copy(
            update={"credential": local_worker_credential(leased.credential)}
        )
    base = account.api_endpoint if runtime == "api" else account.base_url
    credential = {**local_worker_credential(leased.credential), "base_url": base}
    settings = leased.settings
    if settings is not None:
        settings = settings.model_copy(update={"base_url": base})
    needs_ctx = "context_tokens" not in credential
    needs_model = settings is not None and settings.model is None
    if needs_ctx or needs_model:
        try:
            found = (await health.discover({account.api_endpoint: account.token}))[
                account.api_endpoint
            ]
        except Exception:
            found = None
        if found is not None and found.ok and found.models:
            model = settings.model if settings and settings.model else account.model
            window = found.window(model)
            if needs_ctx and window:
                credential["context_tokens"] = window
            if needs_model and settings is not None:
                settings = settings.model_copy(update={"model": found.models[0]})
    return leased.model_copy(update={"credential": credential, "settings": settings})
