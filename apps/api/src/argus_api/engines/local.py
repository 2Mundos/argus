"""The local model, reached only through Argus (engines spec 3): probe what the
server really serves, and run one bounded completion checked by criteria."""

import asyncio
import time

import httpx2 as httpx
from argus_core.criteria_safe import check_answer
from argus_core.custom_accounts import check_base_url
from argus_core.engines import (
    LocalAttempt,
    LocalModelInfo,
    LocalRunRequest,
    LocalRunResult,
)

PROBE_TTL_S = 30.0
QUEUE_WAIT_S = 5.0


def validate_url(url: str) -> str:
    """http(s) only, no embedded credentials, no query/fragment."""
    return check_base_url(url, "ARGUS_LOCAL_MODEL_URL")


def build_chat_completion_payload(req: LocalRunRequest, model: str) -> dict:
    messages = []
    if req.system:
        messages.append({"role": "system", "content": req.system})
    messages.append({"role": "user", "content": req.prompt})
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": req.max_output_tokens,
        "temperature": 0,
    }
    if req.criteria.json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "strict": True,
                "schema": req.criteria.json_schema,
            },
        }
    return body


def check(text: str | None, criteria: object) -> list[str]:
    """Re-export for tests."""
    return check_answer(text, criteria)


class LocalModel:
    """One configured endpoint and one allowed model. The server has one slot, so
    calls are serialised; a caller that cannot get the slot in time is told so."""

    def __init__(
        self,
        url: str,
        model: str,
        *,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = validate_url(url)
        self.model = model
        self.timeout_s = timeout_s
        self._transport = transport
        self._lock = asyncio.Lock()
        self._info: LocalModelInfo | None = None
        self._info_at = 0.0

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url, timeout=timeout, transport=self._transport
        )

    async def probe(self, *, fresh: bool = False) -> LocalModelInfo:
        if (
            not fresh
            and self._info is not None
            and time.monotonic() - self._info_at < PROBE_TTL_S
        ):
            return self._info
        info = await self._probe()
        self._info, self._info_at = info, time.monotonic()
        return info

    async def _probe(self) -> LocalModelInfo:
        try:
            async with self._client(5.0) as c:
                models = (await c.get("/v1/models")).raise_for_status().json()
                served = next(
                    (m for m in models.get("data", []) if m.get("id") == self.model),
                    None,
                )
                if served is None:
                    ids = [m.get("id") for m in models.get("data", [])]
                    return LocalModelInfo(
                        reachable=True,
                        model=ids[0] if ids else None,
                        error=f"{self.model!r} is not served (serves {ids})",
                    )
                n_ctx = (served.get("meta") or {}).get("n_ctx")
                slots = None
                props = await c.get("/props")  # llama.cpp style; optional
                if props.status_code == 200:
                    body = props.json()
                    slots = body.get("total_slots")
                    n_ctx = n_ctx or (
                        body.get("default_generation_settings") or {}
                    ).get("n_ctx")
        except (httpx.HTTPError, ValueError) as exc:
            return LocalModelInfo(reachable=False, error=f"{type(exc).__name__}: {exc}")
        return LocalModelInfo(
            reachable=True, model=self.model, n_ctx=n_ctx, slots=slots
        )

    async def _complete(self, req: LocalRunRequest) -> LocalAttempt:
        body = build_chat_completion_payload(req, self.model)
        started = time.monotonic()
        try:
            async with self._client(self.timeout_s) as c:
                res = await c.post("/v1/chat/completions", json=body)
            res.raise_for_status()
            data = res.json()
            choice = data["choices"][0]
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            return LocalAttempt(
                text=None,
                finish_reason=None,
                prompt_tokens=None,
                completion_tokens=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                failures=[f"call failed: {type(exc).__name__}: {exc}"],
            )
        usage = data.get("usage") or {}
        text = (choice.get("message") or {}).get("content")
        failures = check_answer(text, req.criteria)
        if choice.get("finish_reason") == "length":
            failures.append("output budget exhausted before the answer finished")
        return LocalAttempt(
            text=text,
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
            failures=failures,
        )

    async def run(self, req: LocalRunRequest, *, escalate_to: str) -> LocalRunResult:
        attempts: list[LocalAttempt] = []
        for _ in range(req.max_attempts):
            try:
                async with asyncio.timeout(QUEUE_WAIT_S):
                    await self._lock.acquire()
            except TimeoutError:
                attempts.append(
                    LocalAttempt(
                        text=None,
                        finish_reason=None,
                        prompt_tokens=None,
                        completion_tokens=None,
                        latency_ms=0,
                        failures=["busy: the local model is serving another request"],
                    )
                )
                break  # waiting longer would not be bounded work
            try:
                attempt = await self._complete(req)
            finally:
                self._lock.release()
            attempts.append(attempt)
            if not attempt.failures:
                return LocalRunResult(
                    verdict="accepted",
                    model=self.model,
                    attempts=attempts,
                    text=attempt.text,
                    escalate_to=None,
                )
        return LocalRunResult(
            verdict="escalate",
            model=self.model,
            attempts=attempts,
            text=None,
            escalate_to=escalate_to,
        )
