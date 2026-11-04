"""Model adapters: plain HTTP to the provider with the leased key
(builtin-orchestrator spec 3.4). No vendor SDK.

`send` classifies a failure only from the HTTP status, headers and the provider's
error `type` or `code` token; error message text is never read, logged or replied.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx2 as httpx
from argus_core.accounts import LOCAL_AUTH_TOKEN, validate_local_base_url
from argus_core.custom_accounts import check_base_url
from argus_core.quota import quota_reset_from_headers

from argus_api.orchestrator_builtin.transcript import ToolCall, Transcript

MAX_OUTPUT_TOKENS = 4096
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_VERSION = "2023-06-01"
CLOUD_TIMEOUT_S = 120.0
LOCAL_TIMEOUT_S = 300.0
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None


@dataclass
class Reply:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None


FailureKind = Literal["quota", "auth", "transient", "too_long", "request"]


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    status: int | None = None
    error_type: str | None = None  # the provider's error token, never its message
    reset: datetime | None = None  # quota: the reset the response states

    @property
    def label(self) -> str:
        """`<status> <type>` or the transport error's type, for replies and logs."""
        parts = [str(self.status)] if self.status else []
        if self.error_type:
            parts.append(self.error_type)
        return " ".join(parts) or "unknown"


@dataclass(frozen=True)
class Endpoint:
    url: str
    headers: dict[str, str]
    timeout: float


class Adapter(Protocol):
    def body(
        self,
        model: str,
        system: str,
        history: list[tuple[str, str]],
        transcript: Transcript,
        tools: list[ToolSpec],
        *,
        use_tools: bool,
    ) -> dict[str, Any]: ...

    def parse(self, data: dict[str, Any]) -> Reply: ...


class LeaseError(Exception):
    """The lease cannot be used: no key, or no usable endpoint."""


def secret_of(provider: str, credential: dict[str, Any]) -> str | None:
    """The key a provider takes, from the leased credential document."""
    if provider == "openai" and credential.get("auth_mode") == "apikey":
        key = credential.get("OPENAI_API_KEY")
    elif provider == "local":
        key = credential.get("auth_token") or LOCAL_AUTH_TOKEN
    else:
        key = credential.get("api_key")
    return key if isinstance(key, str) and key.strip() else None


def endpoint(lease: dict[str, Any]) -> Endpoint:
    """Where and how to call the leased account (3.4). The Anthropic and OpenAI
    hosts are fixed; custom and local follow their account's URL rule."""
    provider = lease["provider"]
    key = secret_of(provider, lease.get("credential") or {})
    if key is None:
        raise LeaseError("the leased account holds no API key")
    if provider == "anthropic":
        return Endpoint(
            ANTHROPIC_URL,
            {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
            CLOUD_TIMEOUT_S,
        )
    auth = {"Authorization": f"Bearer {key}"}
    if provider == "openai":
        return Endpoint(OPENAI_URL, auth, CLOUD_TIMEOUT_S)
    settings = lease.get("settings") or {}
    try:
        if provider == "custom":
            base = check_base_url(str(settings.get("base_url") or ""), echo=False)
            return Endpoint(f"{base}/chat/completions", auth, CLOUD_TIMEOUT_S)
        if provider == "local":
            base = validate_local_base_url(settings.get("base_url"))
            return Endpoint(f"{base}/v1/chat/completions", auth, LOCAL_TIMEOUT_S)
    except ValueError as exc:
        raise LeaseError("the leased account has no usable base_url") from exc
    raise LeaseError(f"provider {provider!r} is not built-in capable")


def _error_type(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        for key in ("code", "type"):
            token = error.get(key)
            if isinstance(token, str) and _TOKEN.fullmatch(token):
                return token
    return None


async def send(
    client: httpx.AsyncClient,
    target: Endpoint,
    adapter: Adapter,
    body: dict[str, Any],
) -> Reply | Failure:
    try:
        res = await client.post(
            target.url, json=body, headers=target.headers, timeout=target.timeout
        )
    except (httpx.HTTPError, OSError) as exc:
        return Failure("transient", error_type=type(exc).__name__)
    status = res.status_code
    if status == 200:
        try:
            return adapter.parse(res.json())
        except (ValueError, KeyError, TypeError, IndexError):
            return Failure("request", status, "unreadable_response")
    try:
        data = res.json()
    except ValueError:
        data = None
    code = _error_type(data)
    if status in (402, 429):
        return Failure("quota", status, code, quota_reset_from_headers(res.headers))
    if status in (401, 403):
        return Failure("auth", status, code)
    if status in (408, 409) or status >= 500:
        return Failure("transient", status, code)
    if status == 400 and code == "context_length_exceeded":
        return Failure("too_long", status, code)
    return Failure("request", status, code)


def parse_arguments(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def adapter_for(lease: dict[str, Any]) -> Adapter:
    from argus_api.orchestrator_builtin.models.anthropic import AnthropicAdapter
    from argus_api.orchestrator_builtin.models.openai_compat import OpenAIAdapter

    provider = lease["provider"]
    if provider == "anthropic":
        return AnthropicAdapter()
    settings = lease.get("settings") or {}
    return OpenAIAdapter(
        token_field="max_completion_tokens" if provider == "openai" else "max_tokens",
        # A custom account's routing options (custom_accounts.chat_body).
        provider_block=settings.get("provider") if provider == "custom" else None,
    )
