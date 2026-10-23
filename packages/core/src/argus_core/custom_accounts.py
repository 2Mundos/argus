"""Custom OpenAI-compatible provider accounts (custom accounts spec).

The API key is the account's only secret (`{"api_key": …}` in the SOPS store); the
base URL, label, model and routing options are plain configuration and live in the
registry. Nothing here ever holds or returns a key.
"""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

PROVIDER = "custom"
OPENROUTER = "openrouter"

# Same shape as the model policy's opaque ids (preferred models spec).
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
# `{author}/{slug}` of a catalogue model; one path segment each, never a traversal.
AUTHOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


class CustomAccountError(ValueError):
    """A custom account document or setting that is not acceptable (names fields)."""


def check_base_url(url: str, what: str = "base_url", *, echo: bool = True) -> str:
    """http(s) only, no embedded credentials, no query/fragment; the trailing slash
    is dropped. The one rule for every operator-named endpoint (the local model
    uses it too). `echo=False` keeps the URL out of the error, for URLs that could
    carry a secret."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        shown = f": {url!r}" if echo else ""
        raise ValueError(f"{what} must be an http(s) URL{shown}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{what} must not carry credentials or a query")
    return url.rstrip("/")


def validate_credential(credential: dict[str, Any]) -> None:
    """`{"api_key": "<token>"}` and nothing else. Errors name fields, never values."""
    if set(credential) != {"api_key"}:
        raise CustomAccountError("custom credential must be exactly {api_key}")
    key = credential["api_key"]
    if (
        not isinstance(key, str)
        or not key.strip()
        or any(c in key for c in "\r\n\x00")
        or key != key.strip()
    ):
        raise CustomAccountError("custom credential needs a non-empty api_key string")


def _string_list(value: Any) -> list[str]:
    """A list, or a comma-separated string, as non-empty trimmed strings."""
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, list | tuple):
        items = list(value)
    else:
        return []
    return [s for item in items if item is not None and (s := str(item).strip())]


def openrouter_provider_block_from_preferences(
    preferences: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """OpenRouter's request-body `provider` block from routing preferences (ported
    from Radar). `allow_fallbacks=False` with an `order` pins `only` to it."""
    if not preferences:
        return None
    order = _string_list(preferences.get("order"))
    only = _string_list(preferences.get("only"))
    quantizations = _string_list(preferences.get("quantizations"))
    allow_fallbacks = preferences.get("allow_fallbacks")
    block: dict[str, Any] = {}
    if order:
        block["order"] = order
    if only:
        block["only"] = only
    elif allow_fallbacks is False and order:
        block["only"] = list(order)
    if quantizations:
        block["quantizations"] = quantizations
    if allow_fallbacks is not None:
        block["allow_fallbacks"] = bool(allow_fallbacks)
    return block or None


class RoutingOptions(BaseModel):
    """OpenRouter provider routing. A list or a comma-separated string is accepted."""

    model_config = {"extra": "forbid"}

    order: list[str] = []
    only: list[str] = []
    quantizations: list[str] = []
    allow_fallbacks: bool | None = None

    @field_validator("order", "only", "quantizations", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, str | list | tuple) or (
            not isinstance(v, str) and any(not isinstance(i, str) for i in v)
        ):
            raise ValueError("must be a list of strings or a comma-separated string")
        return _string_list(v)

    def provider_block(self) -> dict[str, Any] | None:
        return openrouter_provider_block_from_preferences(self.model_dump())


class RoleSettings(BaseModel):
    """What one lease role (`engine`: the NanoClaw harness, `executor`: the code
    executor) uses on this account."""

    model_config = {"extra": "forbid"}

    model: str | None = Field(default=None, max_length=200)
    routing_options: RoutingOptions | None = None

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is not None and not MODEL_ID.match(v):
            raise ValueError("model must be an id like author/slug (no spaces)")
        return v


class CustomConfig(BaseModel):
    """The non-secret configuration of a `custom` account."""

    model_config = {"extra": "forbid"}

    base_url: str = Field(max_length=500)
    label: str
    engine: RoleSettings = RoleSettings()
    executor: RoleSettings = RoleSettings()

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        return check_base_url(v, "base_url", echo=False)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not LABEL.match(v):
            raise ValueError("label must match [a-z0-9][a-z0-9_-]{0,49}")
        return v

    @model_validator(mode="after")
    def _routing_is_openrouter(self) -> "CustomConfig":
        if self.label != OPENROUTER and any(
            r.routing_options is not None for r in (self.engine, self.executor)
        ):
            raise ValueError("routing_options are supported for label openrouter only")
        return self

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    def role(self, role: str) -> RoleSettings:
        return self.executor if role == "executor" else self.engine


def serves_role(provider: str, config: CustomConfig | None, role: str) -> bool:
    """May an account be leased for `role`? A native provider carries no config and
    is not restricted. A custom account serves a role only when that role has a
    model: `engine` configured and `executor` empty serves the harness, never a
    delegated CLI. One with no model in either role, or no config, serves neither
    (a request without a model cannot be made)."""
    if provider != PROVIDER:
        return True
    return config is not None and config.role(role).model is not None


class LeaseSettings(BaseModel):
    """What a lease of a custom account adds to the credential: where to call,
    which model, and the request-body `provider` block. No secret."""

    base_url: str
    label: str
    model: str | None
    provider: dict[str, Any] | None


def lease_settings(config: CustomConfig, role: str) -> LeaseSettings:
    chosen = config.role(role)
    return LeaseSettings(
        base_url=config.base_url,
        label=config.label,
        model=chosen.model,
        provider=chosen.routing_options.provider_block()
        if chosen.routing_options
        else None,
    )


def chat_body(
    settings: LeaseSettings, messages: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    """An OpenAI-compatible chat completion body: the model, and the `provider`
    block when routing options are set."""
    if settings.model is None:
        raise CustomAccountError("no model is configured for this role")
    body: dict[str, Any] = {"model": settings.model, "messages": messages, **extra}
    if settings.provider:
        body["provider"] = settings.provider
    return body


class ModelEndpoint(BaseModel):
    """One host serving a model (OpenRouter `…/endpoints`), for the routing pickers."""

    provider_name: str
    tag: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    max_completion_tokens: int | None = None
    prompt_price: str | None = None  # USD per token, as the provider states it
    completion_price: str | None = None
    status: str | None = None


class ModelEndpoints(BaseModel):
    model: str
    endpoints: list[ModelEndpoint]


def parse_endpoints(model: str, payload: Any) -> ModelEndpoints:
    """Normalise OpenRouter's `models/{author}/{slug}/endpoints` answer."""
    data = payload.get("data") if isinstance(payload, dict) else None
    raw = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise CustomAccountError("unexpected endpoints response")

    def text(v: Any) -> str | None:
        return v if isinstance(v, str) and v else None

    def num(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    out = []
    for e in raw:
        if not isinstance(e, dict) or not text(e.get("provider_name")):
            continue
        pricing = e.get("pricing") if isinstance(e.get("pricing"), dict) else {}
        status = e.get("status")
        out.append(
            ModelEndpoint(
                provider_name=e["provider_name"],
                tag=text(e.get("tag")),
                quantization=text(e.get("quantization")),
                context_length=num(e.get("context_length")),
                max_completion_tokens=num(e.get("max_completion_tokens")),
                prompt_price=text(pricing.get("prompt")),
                completion_price=text(pricing.get("completion")),
                status=str(status) if isinstance(status, str | int) else None,
            )
        )
    return ModelEndpoints(model=model, endpoints=out)
