"""A local lease names its runtime's endpoint and the model's context window, from
the account's own probe (local harness spec, "One endpoint per runtime")."""

import httpx2 as httpx
import pytest
from argus_api.accounts.local_health import LocalHealth, deliver
from argus_core.accounts import AccountLease, local_credential_problem
from argus_core.custom_accounts import LeaseSettings

BASE = {
    "base_url": "http://host.docker.internal:8080",
    "auth_token": "argus-local-harness",
}


class Server:
    """A fake model server: answers `/v1/models` per host, counts requests."""

    def __init__(self, models: dict | None = None, *, fail: bool = False) -> None:
        self.models = models or {"data": [{"id": "m", "meta": {"n_ctx": 65536}}]}
        self.fail = fail
        self.seen: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        if self.fail:
            raise httpx.ConnectError("closed")
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=self.models)
        return httpx.Response(404)


def _health(server: Server, *, container: bool = False) -> LocalHealth:
    return LocalHealth(
        transport=httpx.MockTransport(server.handle), container=container
    )


def _lease(provider: str = "local", model: str | None = None, **cred) -> AccountLease:
    credential = {**BASE, **cred} if provider == "local" else {"api_key": "x"}
    settings = (
        LeaseSettings(
            base_url=credential["base_url"], label="local", model=model, provider=None
        )
        if provider == "local"
        else None
    )
    return AccountLease(
        lease_id="l",
        account_id="a",
        name="strata",
        provider=provider,
        kind="api_key",
        credential=credential,
        settings=settings,
    )


async def test_a_worker_lease_keeps_the_worker_endpoint_and_drops_api_only_keys() -> (
    None
):
    server = Server()
    got = await deliver(
        _health(server), _lease(api_base_url="http://127.0.0.1:9000"), "worker"
    )
    # Exactly the keys a deployed NanoClaw accepts, on the worker endpoint.
    assert got.credential == {**BASE, "context_tokens": 65536}
    assert got.settings is not None
    assert got.settings.base_url == "http://host.docker.internal:8080"
    # The window came from the account's own API endpoint, not another one.
    assert server.seen == ["http://127.0.0.1:9000/v1/models"]


@pytest.mark.parametrize(
    ("container", "endpoint"),
    [
        (False, "http://127.0.0.1:8080"),  # host API, Docker workers
        (True, "http://host.docker.internal:8080"),  # API in a container
    ],
)
async def test_a_builtin_lease_gets_the_api_endpoint(container, endpoint) -> None:
    server = Server()
    got = await deliver(_health(server, container=container), _lease(), "api")
    assert got.credential["base_url"] == endpoint
    assert got.settings is not None and got.settings.base_url == endpoint
    assert got.settings.model == "m"  # the served model, the account names none
    assert server.seen == [f"{endpoint}/v1/models"]


async def test_the_account_setting_wins_over_the_probe() -> None:
    server = Server()
    got = await deliver(
        _health(server), _lease(model="qwen3", context_tokens=32768), "worker"
    )
    assert got.credential["context_tokens"] == 32768
    assert got.settings is not None and got.settings.model == "qwen3"
    assert server.seen == []  # nothing to fill: no probe


@pytest.mark.parametrize(
    "server",
    [Server({"data": [{"id": "m"}]}), Server(fail=True), Server({"data": "nope"})],
    ids=["no-window", "unreachable", "unreadable"],
)
async def test_an_unknown_window_stays_absent_and_never_fails_the_lease(server) -> None:
    got = await deliver(_health(server), _lease(), "worker")
    assert "context_tokens" not in got.credential


async def test_cloud_leases_are_untouched() -> None:
    server = Server()
    got = await deliver(_health(server), _lease("anthropic"), "api")
    assert got.credential == {"api_key": "x"} and server.seen == []


@pytest.mark.parametrize(
    ("value", "ok"),
    [(65536, True), (4096, True), (1024, False), (True, False), ("65536", False)],
)
def test_context_tokens_is_validated(value, ok) -> None:
    problem = local_credential_problem({**BASE, "context_tokens": value})
    assert (problem is None) is ok
