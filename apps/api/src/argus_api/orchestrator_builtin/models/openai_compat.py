"""OpenAI-compatible Chat Completions rendering and parsing: OpenAI, custom and
local accounts (builtin-orchestrator 3.4, 3.9)."""

import json
from typing import Any

from argus_api.orchestrator_builtin.models import (
    MAX_OUTPUT_TOKENS,
    Reply,
    ToolSpec,
    Usage,
    parse_arguments,
)
from argus_api.orchestrator_builtin.transcript import (
    Result,
    Step,
    ToolCall,
    Transcript,
    UserText,
)


class OpenAIAdapter:
    def __init__(
        self, *, token_field: str, provider_block: dict[str, Any] | None = None
    ) -> None:
        self.token_field = token_field
        self.provider_block = provider_block

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
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages += [{"role": role, "content": text} for role, text in history]
        for item in transcript.items:
            if isinstance(item, UserText):
                if messages[-1]["role"] == "user":
                    messages[-1]["content"] += f"\n\n{item.text}"
                else:
                    messages.append({"role": "user", "content": item.text})
            elif isinstance(item, Step):
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": item.text or None,
                }
                if item.calls:
                    msg["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            **(
                                {"extra_content": call.extra_content}
                                if call.extra_content
                                else {}
                            ),
                            "function": {
                                "name": call.name,
                                "arguments": call.raw
                                or json.dumps(call.arguments or {}),
                            },
                        }
                        for call in item.calls
                    ]
                messages.append(msg)
            elif isinstance(item, Result):
                text = f"ERROR {item.text}" if item.is_error else item.text
                messages.append(
                    {"role": "tool", "tool_call_id": item.call_id, "content": text}
                )
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            self.token_field: MAX_OUTPUT_TOKENS,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto" if use_tools else "none"
        if self.provider_block:
            body["provider"] = self.provider_block
        return body

    def parse(self, data: dict[str, Any]) -> Reply:
        message = data["choices"][0]["message"]
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw["function"]
            arguments = fn.get("arguments")
            text = arguments if isinstance(arguments, str) else json.dumps(arguments)
            extra = raw.get("extra_content")
            calls.append(
                ToolCall(
                    id=str(raw["id"]),
                    name=str(fn["name"]),
                    arguments=parse_arguments(text),
                    raw=text,
                    extra_content=extra if isinstance(extra, dict) else None,
                )
            )
        content = message.get("content")
        return Reply(
            content if isinstance(content, str) else "",
            calls,
            _usage(data.get("usage")),
        )


def _usage(raw: Any) -> Usage | None:
    """`prompt_tokens` counts cached input too: cached is taken out of it."""
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    out = raw.get("completion_tokens")
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    cached = cached if isinstance(cached, int) else None
    if not isinstance(prompt, int):
        prompt = None
    uncached = None if prompt is None else prompt - (cached or 0)
    return Usage(uncached, out if isinstance(out, int) else None, cached)
