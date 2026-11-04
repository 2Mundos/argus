"""The loop's one engine lease, failover and model choice
(builtin-orchestrator spec 3.4, 3.8).

Every turn reads the current lease when a step starts, so a failover in one turn
moves every turn to the new account at its next step.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from argus_api.orchestrator_builtin.client import ArgusClient
from argus_api.orchestrator_builtin.models import Failure, secret_of

PROVIDERS = ["anthropic", "openai", "custom", "local"]
LEASE_BODY = {
    "role": "engine",
    "runtime_providers": PROVIDERS,
    "credential_kinds": ["api_key"],
}
# Engines of the host model policy whose harness chain names a provider's model.
POLICY_ENGINE = {"anthropic": "claude", "openai": "codex"}
# Context windows when the account states none (3.7).
DEFAULT_WINDOW = {"anthropic": 200_000}
FALLBACK_WINDOW = 128_000
BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0

Sleep = Callable[[float], Awaitable[None]]
Log = Callable[[str], None]


@dataclass(frozen=True)
class Lease:
    raw: dict[str, Any]
    model: str | None  # None: no model is configured for this provider

    @property
    def id(self) -> str:
        return self.raw["lease_id"]

    @property
    def account_id(self) -> str:
        return self.raw["account_id"]

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def provider(self) -> str:
        return self.raw["provider"]

    @property
    def secret(self) -> str | None:
        return secret_of(self.provider, self.raw.get("credential") or {})

    @property
    def context_tokens(self) -> int | None:
        """Known only for local accounts: stated, or measured by the probe."""
        if self.provider != "local":
            return None
        ctx = (self.raw.get("credential") or {}).get("context_tokens")
        return ctx if isinstance(ctx, int) else None

    @property
    def window(self) -> int:
        return self.context_tokens or DEFAULT_WINDOW.get(self.provider, FALLBACK_WINDOW)


@dataclass(frozen=True)
class NoAccount:
    earliest_reset: datetime | None


class Engine:
    def __init__(self, argus: ArgusClient, log: Log) -> None:
        self.argus = argus
        self.log = log
        self.lease: Lease | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Lease the best built-in-capable account; False when none is usable."""
        res = await self.argus.rest("POST", "/accounts/lease", json=LEASE_BODY)
        if res.status == 409:
            self.lease = None
            return False
        if not res.ok:
            raise RuntimeError(f"lease answered {res.status} {res.code}")
        self.lease = Lease(res.body, await self._model(res.body))
        self.log(
            f"leased {self.lease.name} ({self.lease.provider},"
            f" {self.lease.model or 'no model'})"
        )
        return True

    async def _model(self, raw: dict[str, Any]) -> str | None:
        """Which model runs on this lease, first match wins (3.4)."""
        provider = raw["provider"]
        if provider in POLICY_ENGINE:
            if raw.get("model"):
                return raw["model"]
            return await self._policy_model(POLICY_ENGINE[provider])
        model = (raw.get("settings") or {}).get("model")
        if not model and provider == "local":
            model = (raw.get("credential") or {}).get("model")
        return model if isinstance(model, str) and model else None

    async def _policy_model(self, engine: str) -> str | None:
        res = await self.argus.rest("GET", "/settings")
        if not res.ok:
            return None
        for item in res.body.get("settings", []):
            if item.get("key") == "models" and isinstance(item.get("value"), dict):
                chain = (item["value"].get("harness") or {}).get(engine) or []
                return chain[0] if chain else None
        return None

    async def wait_for_lease(self, stopping: asyncio.Event, sleep: Sleep) -> bool:
        """Lease, retrying after 5 s doubling up to 5 min while none is usable.
        False when asked to stop first."""
        delay = BACKOFF_START_S
        while not stopping.is_set():
            if await self.acquire():
                return True
            await sleep_or_stop(sleep, delay, stopping)
            delay = min(delay * 2, BACKOFF_MAX_S)
        return False

    async def failover(self, seen: Lease, failure: Failure) -> Lease | NoAccount:
        """A 402/429 on `seen`: mark it exhausted and take the next account. A turn
        that finds another turn already switched takes the new lease."""
        async with self._lock:
            if self.lease is not seen:
                return self.lease or NoAccount(None)
            reset = failure.reset
            report: dict[str, Any] = {
                "role": "engine",
                "runtime_providers": PROVIDERS,
                "credential_kinds": ["api_key"],
                "source": "structured",
                "reason_code": "quota_exhausted",
                "reason": f"HTTP {failure.status} from the model provider",
                "reset_status": "stated" if reset else "absent",
                "until": reset.isoformat() if reset else None,
            }
            res = await self.argus.rest(
                "POST", f"/accounts/{seen.account_id}/exhausted", json=report
            )
            self.lease = None
            answer = res.body if res.ok and isinstance(res.body, dict) else {}
            if answer.get("next_account_id") and await self.acquire():
                assert self.lease is not None
                self.log(f"failover from {seen.name} to {self.lease.name}")
                return self.lease
            raw = answer.get("earliest_reset")
            return NoAccount(datetime.fromisoformat(raw) if raw else None)

    async def auth_rejected(self, seen: Lease) -> None:
        """A 401/403: record it, release the lease. The account is not exhausted."""
        async with self._lock:
            if self.lease is not seen:
                return
            await self.argus.rest(
                "POST",
                f"/accounts/{seen.account_id}/auth-rejected",
                json={"role": "engine", "source": "structured"},
            )
            await self.argus.rest(
                "POST", "/accounts/release", params={"role": "engine"}
            )
            self.lease = None


async def sleep_or_stop(sleep: Sleep, seconds: float, stopping: asyncio.Event) -> None:
    nap = asyncio.ensure_future(sleep(seconds))
    stop = asyncio.ensure_future(stopping.wait())
    try:
        await asyncio.wait({nap, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        nap.cancel()
        stop.cancel()
