"""A local lease tells the harness the model's context window (local harness spec)."""

from types import SimpleNamespace

import pytest
from argus_api.accounts.routes import _with_local_context
from argus_core.accounts import AccountLease, local_credential_problem
from argus_core.engines import LocalModelInfo

BASE = {
    "base_url": "http://host.docker.internal:8080",
    "auth_token": "argus-local-harness",
}


class FakeLocal:
    def __init__(self, n_ctx: int | None, fail: bool = False) -> None:
        self.n_ctx, self.fail, self.calls = n_ctx, fail, 0

    async def probe(self) -> LocalModelInfo:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return LocalModelInfo(reachable=True, model="m", n_ctx=self.n_ctx)


def _request(local: FakeLocal | None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(local_model=local))
    )


def _lease(provider: str = "local", **cred) -> AccountLease:
    return AccountLease(
        lease_id="l",
        account_id="a",
        name="strata",
        provider=provider,
        kind="api_key",
        credential={**BASE, **cred} if provider == "local" else {"api_key": "x"},
    )


async def test_probe_fills_the_window_when_the_account_names_none() -> None:
    got = await _with_local_context(_request(FakeLocal(65536)), _lease())
    assert got.credential["context_tokens"] == 65536


async def test_the_account_setting_wins_over_the_probe() -> None:
    local = FakeLocal(65536)
    got = await _with_local_context(_request(local), _lease(context_tokens=32768))
    assert got.credential["context_tokens"] == 32768 and local.calls == 0


@pytest.mark.parametrize("local", [None, FakeLocal(None), FakeLocal(1, fail=True)])
async def test_unknown_window_stays_absent(local) -> None:
    got = await _with_local_context(_request(local), _lease())
    assert "context_tokens" not in got.credential


async def test_cloud_leases_are_untouched() -> None:
    local = FakeLocal(65536)
    got = await _with_local_context(_request(local), _lease("anthropic"))
    assert got.credential == {"api_key": "x"} and local.calls == 0


@pytest.mark.parametrize(
    ("value", "ok"),
    [(65536, True), (4096, True), (1024, False), (True, False), ("65536", False)],
)
def test_context_tokens_is_validated(value, ok) -> None:
    problem = local_credential_problem({**BASE, "context_tokens": value})
    assert (problem is None) is ok
