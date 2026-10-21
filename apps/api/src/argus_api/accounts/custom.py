"""Calls to a custom account's OpenAI-compatible endpoint, made by Argus on behalf
of the dashboard: the readiness probe and OpenRouter's model endpoints (custom
accounts spec 3). The key goes only to the account's own base URL, redirects are
never followed, and nothing from the upstream body or the URL reaches an error."""

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx2 as httpx
from argus_core.custom_accounts import (
    AUTHOR,
    OPENROUTER,
    SLUG,
    CustomAccountError,
    CustomConfig,
    ModelEndpoints,
    parse_endpoints,
)
from argus_core.quota import classify_quota, quota_notice_from_response
from pydantic import BaseModel

from argus_api.errors import DomainError, ValidationFailedError

log = logging.getLogger(__name__)

TIMEOUT_S = 15.0
MAX_MODELS = 2000
MAX_BODY = 8 * 1024 * 1024

ProbeStatus = Literal["ok", "unauthorized", "exhausted", "unreachable", "failed"]


class CustomProbe(BaseModel):
    """The readiness of a custom account: `GET {base_url}/models` with the key."""

    status: ProbeStatus
    http_status: int | None = None
    reason: str  # fixed vocabulary, never upstream text
    models: list[str] = []  # suggestions for the model field
    exhausted_until: datetime | None = None


def _client(
    base_url: str, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=TIMEOUT_S,
        transport=transport,
        follow_redirects=False,
    )


def _model_ids(payload: Any) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    ids = {
        m["id"]
        for m in (data if isinstance(data, list) else [])
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    }
    return sorted(ids)[:MAX_MODELS]


def _classify(status: int, headers: Mapping[str, str]) -> CustomProbe:
    notice = quota_notice_from_response(status, headers)
    if notice is not None:
        hit = classify_quota(notice, "custom")
        return CustomProbe(
            status="exhausted",
            http_status=status,
            reason="credits exhausted" if status == 402 else "rate limited",
            exhausted_until=hit.until if hit else None,
        )
    if status in (401, 403):
        return CustomProbe(
            status="unauthorized", http_status=status, reason="key rejected"
        )
    return CustomProbe(
        status="failed", http_status=status, reason=f"unexpected HTTP {status}"
    )


async def probe(
    config: CustomConfig,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CustomProbe:
    """`GET {base_url}/models` with the key as bearer. A 402/429 reports the
    account exhausted (with the reset when the headers give one); the probe itself
    changes nothing."""
    try:
        async with _client(config.base_url, transport) as client:
            res = await client.get(
                "/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as exc:
        log.warning("custom probe failed: %s", type(exc).__name__)
        return CustomProbe(status="unreachable", reason=type(exc).__name__)
    if res.status_code != 200:
        return _classify(res.status_code, res.headers)
    try:
        models = _model_ids(res.json()) if len(res.content) <= MAX_BODY else []
    except ValueError:
        return CustomProbe(
            status="failed", http_status=200, reason="response is not JSON"
        )
    return CustomProbe(status="ok", http_status=200, reason="ok", models=models)


async def model_endpoints(
    config: CustomConfig,
    author: str,
    slug: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelEndpoints:
    """The hosts serving `author/slug` (hosts, quantization, context, price), from
    OpenRouter's `models/{author}/{slug}/endpoints`. Public data: no key is sent."""
    if config.label != OPENROUTER:
        raise ValidationFailedError("model endpoints are available for openrouter only")
    if not AUTHOR.match(author) or not SLUG.match(slug):
        raise ValidationFailedError("author and slug must be a catalogue model id")
    path = f"/models/{quote(author, safe='')}/{quote(slug, safe='')}/endpoints"
    try:
        async with _client(config.base_url, transport) as client:
            res = await client.get(path)
    except httpx.HTTPError as exc:
        log.warning("custom endpoints failed: %s", type(exc).__name__)
        raise UpstreamError(f"upstream unreachable ({type(exc).__name__})") from None
    if res.status_code == 404:
        raise UpstreamError("model not found upstream", status=404)
    if res.status_code != 200 or len(res.content) > MAX_BODY:
        raise UpstreamError(f"upstream answered HTTP {res.status_code}")
    try:
        return parse_endpoints(f"{author}/{slug}", res.json())
    except (ValueError, CustomAccountError):
        raise UpstreamError("upstream response not understood") from None


class UpstreamError(DomainError):
    """The provider could not answer; the message is ours, never the provider's."""

    status = 502
    code = "upstream_error"

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = 404 if status == 404 else 502
