"""Anthropic Messages API rendering and parsing (builtin-orchestrator 3.4, 3.9)."""

from typing import Any

from argus_api.orchestrator_builtin.models import (
    MAX_OUTPUT_TOKENS,
    Reply,
    ToolSpec,
    Usage,
)
from argus_api.orchestrator_builtin.transcript import (
    Result,
    Step,
    ToolCall,
    Transcript,
    UserText,
)

_EPHEMERAL = {"type": "ephemeral"}


def _merge(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Alternating roles: consecutive same-role messages become one."""
    merged: list[dict[str, Any]] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"].extend(msg["content"])
        else:
            merged.append({"role": msg["role"], "content": list(msg["content"])})
    return merged


class AnthropicAdapter:
    def body(
        self,
        model: str,
        system: str,
        history: list[tuple[str, str]],
        transcript: Transcript,
        tools: list[ToolSpec],
        *,
        use_tools: bool,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": role, "content": [{"type": "text", "text": text}]}
            for role, text in history
        ]
        for item in transcript.items:
            if isinstance(item, UserText):
                messages.append(
                    {"role": "user", "content": [{"type": "text", "text": item.text}]}
                )
            elif isinstance(item, Step):
                content: list[dict[str, Any]] = []
                if item.text:
                    content.append({"type": "text", "text": item.text})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments if call.arguments is not None else {},
                    }
                    for call in item.calls
                )
                messages.append({"role": "assistant", "content": content})
            elif isinstance(item, Result):
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": item.call_id,
                                "content": item.text,
                                "is_error": item.is_error,
                            }
                        ],
                    }
                )
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            # Cached across the steps of a turn: the persona, skill and context.
            "system": [{"type": "text", "text": system, "cache_control": _EPHEMERAL}],
            "messages": _merge(messages),
        }
        if tools:
            specs = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]
            specs[-1] = {**specs[-1], "cache_control": _EPHEMERAL}
            body["tools"] = specs
            # A transcript with tool calls needs the tools defined; the last call
            # of a turn may not use them.
            body["tool_choice"] = {"type": "auto" if use_tools else "none"}
        return body

    def parse(self, data: dict[str, Any]) -> Reply:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in data["content"]:
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                arguments = block.get("input")
                calls.append(
                    ToolCall(
                        id=str(block["id"]),
                        name=str(block["name"]),
                        arguments=arguments if isinstance(arguments, dict) else None,
                        raw="",
                    )
                )
        usage = data.get("usage")
        return Reply("\n".join(t for t in texts if t), calls, _usage(usage))


def _usage(raw: Any) -> Usage | None:
    """Uncached input is `input_tokens` plus cache writes; cache reads are cached."""
    if not isinstance(raw, dict):
        return None

    def num(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) else None

    base, writes = num("input_tokens"), num("cache_creation_input_tokens")
    uncached = None if base is None and writes is None else (base or 0) + (writes or 0)
    return Usage(uncached, num("output_tokens"), num("cache_read_input_tokens"))
